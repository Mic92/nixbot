"""Per-derivation build-log container.

Layout: ``<compressed frames> <toc json> <u32 toc_len> <NBL1>``. The reader
tail-parses the TOC, so a derivation renders by decompressing only its
group frame and slicing its byte range. Small derivations share a frame to
keep zstd's ratio while staying addressable. Level 12, no trained dict:
logs are small, so tune write CPU / read latency, not ratio.
"""

from __future__ import annotations

import bisect
import json
import struct
from dataclasses import dataclass, field

import zstandard

MAGIC = b"NBL1"
_MAGIC_LEN = len(MAGIC)
_LEVEL = 12
_GROUP_BYTES = 64 * 1024


@dataclass
class _Drv:
    name: str
    status: str = "built"
    lines: list[str] = field(default_factory=list)
    phases: list[list] = field(default_factory=list)  # [name, first_line]
    t0: int | None = None
    t1: int | None = None


class LogContainerWriter:
    """Accumulate ``(drv, line, phase, ts)`` records, pack on ``finalize``.

    Interleaved derivations are buffered contiguously, keyed by drv path,
    and laid out in first-seen order. Holds the size-capped log in memory.
    """

    def __init__(self, level: int = _LEVEL, group_bytes: int = _GROUP_BYTES) -> None:
        self._level = level
        self._group_bytes = group_bytes
        self._drvs: dict[str, _Drv] = {}

    def _get(self, drv: str, name: str | None = None) -> _Drv:
        d = self._drvs.get(drv)
        if d is None:
            d = self._drvs[drv] = _Drv(name=name or drv)
        elif name is not None:
            d.name = name
        return d

    def register(self, drv: str, name: str | None = None) -> None:
        """Ensure a derivation exists (and set its name) before any line."""
        self._get(drv, name)

    def line(
        self, drv: str, text: str, ts: int | None = None, name: str | None = None
    ) -> None:
        d = self._get(drv, name)
        if ts is not None:
            if d.t0 is None:
                d.t0 = ts
            d.t1 = ts
        d.lines.append(text)

    def phase(self, drv: str, phase: str, ts: int | None = None) -> None:
        d = self._get(drv)
        if not d.phases or d.phases[-1][0] != phase:
            d.phases.append([phase, len(d.lines)])
        if ts is not None:
            d.t1 = ts

    def status(self, drv: str, status: str) -> None:
        self._get(drv).status = status

    def stop(self, drv: str, ts: int) -> None:
        self._get(drv).t1 = ts

    def finalize(self) -> bytes:
        c = zstandard.ZstdCompressor(level=self._level)
        frames: list[bytes] = []
        toc: list[dict] = []
        off = 0
        buf: list[bytes] = []
        members: list[dict] = []
        bufbytes = 0

        def flush() -> None:
            nonlocal off, buf, members, bufbytes
            if not buf:
                return
            fr = c.compress(b"".join(buf))
            for e in members:
                e["off"], e["clen"] = off, len(fr)
            frames.append(fr)
            off += len(fr)
            buf, members, bufbytes = [], [], 0

        for d in self._drvs.values():
            txt = "".join(t + "\n" for t in d.lines).encode()
            e = {
                "name": d.name,
                "status": d.status,
                "off": 0,
                "clen": 0,
                "bs": bufbytes,
                "bn": len(txt),
                "n": len(d.lines),
                "ph": d.phases,
                "t0": d.t0,
                "t1": d.t1,
            }
            toc.append(e)
            members.append(e)
            buf.append(txt)
            bufbytes += len(txt)
            if bufbytes >= self._group_bytes:
                flush()
        flush()

        payload = b"".join(frames)
        tj = json.dumps(toc, separators=(",", ":")).encode()
        return payload + tj + struct.pack("<I", len(tj)) + MAGIC


def is_container(blob: bytes) -> bool:
    return len(blob) >= _MAGIC_LEN and blob[-_MAGIC_LEN:] == MAGIC


class LogContainerReader:
    """Random-access reader; decompresses one group frame (cached) per drv."""

    def __init__(self, blob: bytes) -> None:
        (tlen,) = struct.unpack("<I", blob[-8:-4])
        self.toc: list[dict] = json.loads(blob[-8 - tlen : -8])
        self._blob = blob
        self._d = zstandard.ZstdDecompressor()
        self._cache_off = -1
        self._cache_raw = b""

    def __len__(self) -> int:
        return len(self.toc)

    def entry(self, i: int) -> dict:
        return self.toc[i]

    def _frame(self, off: int, clen: int) -> bytes:
        if off != self._cache_off:
            self._cache_raw = self._d.decompress(self._blob[off : off + clen])
            self._cache_off = off
        return self._cache_raw

    def lines(self, i: int) -> list[str]:
        e = self.toc[i]
        raw = self._frame(e["off"], e["clen"])
        return raw[e["bs"] : e["bs"] + e["bn"]].decode().splitlines()

    def search(self, query: str, per_drv_cap: int = 100) -> list[dict]:
        """Case-insensitive scan, grouped by drv. Fast-rejects frames
        lacking the term; attributes matches by byte bisect. No index."""
        qb = query.lower().encode()
        toc = self.toc
        groups: dict[int, list[tuple[int, int]]] = {}
        for i, e in enumerate(toc):
            groups.setdefault(e["off"], []).append((e["bs"], i))
        hits: dict[int, dict] = {}
        for off, mem in groups.items():
            mem.sort()
            starts = [bs for bs, _ in mem]
            clen = toc[mem[0][1]]["clen"]
            raw = self._d.decompress(self._blob[off : off + clen]).lower()
            if qb not in raw:
                continue
            linebase, acc = [], 0
            for _, ti in mem:
                linebase.append(acc)
                acc += toc[ti]["n"]
            last_pos = last_line = 0
            pos = raw.find(qb)
            while pos != -1:
                last_line += raw.count(b"\n", last_pos, pos)
                last_pos = pos
                mi = bisect.bisect_right(starts, pos) - 1
                ti = mem[mi][1]
                h = hits.setdefault(
                    ti, {"idx": ti, "name": toc[ti]["name"], "lines": []}
                )
                if len(h["lines"]) < per_drv_cap:
                    # 1-based, matching the rendered line numbers.
                    h["lines"].append(last_line - linebase[mi] + 1)
                pos = raw.find(qb, pos + 1)
        return [hits[k] for k in sorted(hits)]
