"""Binary-cache uploads through one persistent queue per uploader.

Every locally built derivation (intermediates included) and each
attribute's final outputs land in `upload_queue` so a restart loses
nothing. Intermediates arrive as .drv paths and silently drop out when
their outputs are invalid (the derivation failed).

Batch uploaders run one push over whatever accumulated. Stream
uploaders keep one long-running child (`niks3 push --stdin`) that
acknowledges each path, so an attribute waits only for its own paths.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from nixbot_effects.proc import ProcessGroup

from .db_gen import uploads as q
from .post_build import InterpolationError, interpolate

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    import asyncpg

    from .config import UploaderConfig

logger = logging.getLogger(__name__)

UPLOAD_TIMEOUT = 60 * 60
MAX_ATTEMPTS = 3
RETRY_DELAY = 30.0
BATCH_LIMIT = 5000
# Well below ARG_MAX (2 MiB incl. environment on Linux).
MAX_ARGS_BYTES = 512 * 1024
STREAM_STOP_GRACE = 10.0


@dataclass
class UploadResult:
    success: bool
    output: str


def chunk_args(args: Sequence[str], limit: int = MAX_ARGS_BYTES) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for arg in args:
        n = len(arg.encode()) + 1
        if current and size + n > limit:
            chunks.append(current)
            current, size = [], 0
        current.append(arg)
        size += n
    if current:
        chunks.append(current)
    return chunks


async def _run(
    cmd: list[str],
    *,
    stdin: bytes | None = None,
    env: dict[str, str] | None = None,
    timeout: float = UPLOAD_TIMEOUT,  # noqa: ASYNC109
    merge_stderr: bool = True,
) -> tuple[int, str]:
    try:
        group = await ProcessGroup.start(
            cmd,
            env=env,
            stdin=asyncio.subprocess.PIPE
            if stdin is not None
            else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT if merge_stderr else None,
        )
    except OSError as e:
        return 127, f"failed to start {cmd[0]!r}: {e}"
    try:
        stdout, _ = await asyncio.wait_for(
            group.proc.communicate(stdin), timeout=timeout
        )
    except TimeoutError:
        await group.reap()
        return 124, f"timed out after {timeout}s"
    except asyncio.CancelledError:
        await group.reap()
        raise
    assert group.proc.returncode is not None  # noqa: S101
    return group.proc.returncode, stdout.decode(errors="replace")


PATH_INFO_CMD = [
    "nix",
    "--extra-experimental-features",
    "nix-command",
    "path-info",
    "--option",
    "substitute",
    "false",
    "--json",
]


def parse_path_info(stdout: str) -> set[str]:
    """Valid paths from `nix path-info --json` (nix >=2.19 emits an
    object with null for invalid paths, older nix a list)."""
    data = json.loads(stdout)
    if isinstance(data, dict):
        return {p for p, info in data.items() if info is not None}
    return {e["path"] for e in data if e.get("valid", True)}


async def resolve_valid_outputs(paths: set[str]) -> set[str]:
    """Map .drv paths to their outputs and drop paths not in the store."""
    args = sorted(f"{p}^*" if p.endswith(".drv") else p for p in paths)
    valid: set[str] = set()
    for chunk in chunk_args(args):
        rc, stdout = await _run([*PATH_INFO_CMD, *chunk], merge_stderr=False)
        if rc != 0:
            logger.warning("nix path-info failed", extra={"rc": rc})
            continue
        valid |= parse_path_info(stdout)
    return valid


@dataclass
class Uploader:
    config: UploaderConfig
    pool: asyncpg.Pool
    timeout: float = UPLOAD_TIMEOUT
    retry_delay: float = RETRY_DELAY
    resolve: Callable[[set[str]], Awaitable[set[str]]] = resolve_valid_outputs
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _buffer: list[str] = field(default_factory=list, init=False)

    @staticmethod
    def create(config: UploaderConfig, pool: asyncpg.Pool, **kwargs: Any) -> Uploader:
        cls = StreamUploader if config.paths_via == "stream" else BatchUploader
        return cls(config, pool, **kwargs)

    @property
    def name(self) -> str:
        return self.config.name

    def enqueue_nowait(self, drv_path: str) -> None:
        """Best-effort upload of a .drv the build log reported as built;
        picked up by the worker on its next iteration."""
        self._buffer.append(drv_path)
        self._wake.set()

    def _take_buffer(self) -> list[str]:
        paths, self._buffer = self._buffer, []
        return paths

    def _command(self) -> tuple[list[str], dict[str, str]]:
        # No %(prop:..)s: one process spans many attributes.
        command = [interpolate(arg, {}) for arg in self.config.command]
        env = {
            **os.environ,
            **{k: interpolate(v, {}) for k, v in self.config.environment.items()},
        }
        return command, env

    async def upload(self, paths: list[str]) -> UploadResult:
        raise NotImplementedError

    async def run(self) -> None:
        raise NotImplementedError


@dataclass
class _Waiter:
    future: asyncio.Future[UploadResult]
    # Highest queue row of this attribute; rows push in id order.
    last_id: int | None = None


@dataclass
class BatchUploader(Uploader):
    """One push per iteration over everything queued (argv or stdin)."""

    _waiters: list[_Waiter] = field(default_factory=list, init=False)

    async def upload(self, paths: list[str]) -> UploadResult:
        """Upload an attribute's final outputs and wait for the first verdict."""
        if not paths:
            return UploadResult(success=True, output="")
        waiter = _Waiter(asyncio.get_running_loop().create_future())
        # Registered before the insert so a batch picking the rows up
        # while we still await the insert finds the waiter.
        self._waiters.append(waiter)
        try:
            ids = await q.enqueue_upload_paths(
                self.pool, uploader=self.name, paths=paths
            )
            waiter.last_id = max(ids)
            self._wake.set()
            return await waiter.future
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    def _settle(self, upto: int, result: UploadResult) -> None:
        for w in list(self._waiters):
            if w.last_id is not None and w.last_id <= upto:
                w.future.set_result(result)
                self._waiters.remove(w)

    async def run(self) -> None:
        while True:
            self._wake.clear()
            if buffered := self._take_buffer():
                await q.enqueue_upload_paths(
                    self.pool, uploader=self.name, paths=buffered
                )
            rows = await q.pending_upload_paths(
                self.pool, uploader=self.name, limit=BATCH_LIMIT
            )
            if not rows:
                await self._wake.wait()
                continue
            ids = [r.id_ for r in rows]
            try:
                result = await self._push({r.path for r in rows})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("uploader crashed", extra={"uploader": self.name})
                result = UploadResult(success=False, output=f"internal error: {e}")
            if result.success:
                await q.delete_upload_paths(self.pool, ids=ids)
            else:
                logger.warning(
                    "upload failed",
                    extra={"uploader": self.name, "output": result.output[-2000:]},
                )
                await q.retry_upload_paths(
                    self.pool, ids=ids, max_attempts=MAX_ATTEMPTS
                )
            self._settle(max(ids), result)
            if not result.success:
                await asyncio.sleep(self.retry_delay)

    async def _push(self, raw: set[str]) -> UploadResult:
        paths = sorted(await self.resolve(raw))
        if not paths:
            return UploadResult(success=True, output="")
        try:
            command, env = self._command()
        except InterpolationError as e:
            return UploadResult(success=False, output=str(e))
        logger.info("uploading", extra={"uploader": self.name, "paths": len(paths)})
        if self.config.paths_via == "stdin":
            rc, out = await _run(
                command,
                stdin=("\n".join(paths) + "\n").encode(),
                env=env,
                timeout=self.timeout,
            )
            return UploadResult(success=rc == 0, output=out)
        outputs: list[str] = []
        ok = True
        for chunk in chunk_args(paths):
            rc, out = await _run([*command, *chunk], env=env, timeout=self.timeout)
            outputs.append(out)
            ok = ok and rc == 0
        return UploadResult(success=ok, output="".join(outputs))


@dataclass
class StreamUploader(Uploader):
    """Long-running child speaking `niks3 push --stdin`: store paths in,
    one JSON `{path, status, message}` line per path out. Queue rows only
    bridge child or service restarts; retrying is the child's job."""

    _pending: defaultdict[str, list[asyncio.Future[UploadResult]]] = field(
        default_factory=lambda: defaultdict(list), init=False
    )

    async def upload(self, paths: list[str]) -> UploadResult:
        loop = asyncio.get_running_loop()
        futures = [loop.create_future() for _ in paths]
        for p, fut in zip(paths, futures, strict=True):
            self._pending[p].append(fut)
        self._buffer.extend(paths)
        self._wake.set()
        try:
            results = await asyncio.wait_for(asyncio.gather(*futures), self.timeout)
        except TimeoutError:
            return UploadResult(
                success=False, output=f"timed out after {self.timeout}s"
            )
        failed = [r.output for r in results if not r.success]
        return UploadResult(success=not failed, output="\n".join(failed))

    async def _ack(self, line: bytes) -> None:
        try:
            msg = json.loads(line)
            path, ok = msg["path"], msg["status"] == "ok"
        except (ValueError, KeyError, TypeError):
            logger.warning(
                "uploader emitted garbage",
                extra={"uploader": self.name, "line": line[:200]},
            )
            return
        result = UploadResult(success=ok, output=str(msg.get("message", "")))
        if not ok:
            logger.warning(
                "upload failed",
                extra={"uploader": self.name, "path": path, "output": result.output},
            )
        for fut in self._pending.pop(path, []):
            if not fut.done():
                fut.set_result(result)
        await q.delete_upload_path(self.pool, uploader=self.name, path=path)

    async def _feed(self, stdin: asyncio.StreamWriter) -> None:
        paths = list(await q.all_pending_upload_paths(self.pool, uploader=self.name))
        while True:
            if paths:
                stdin.write("".join(f"{p}\n" for p in paths).encode())
                await stdin.drain()
            await self._wake.wait()
            self._wake.clear()
            paths = sorted(await self.resolve(set(self._take_buffer())))
            if paths:
                await q.enqueue_upload_paths(self.pool, uploader=self.name, paths=paths)

    async def _run_child(self) -> None:
        command, env = self._command()
        group = await ProcessGroup.start(
            command,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        proc = group.proc
        assert proc.stdin is not None  # noqa: S101
        assert proc.stdout is not None  # noqa: S101
        logger.info("uploader started", extra={"uploader": self.name})
        feed = asyncio.create_task(self._feed(proc.stdin))
        try:
            async for line in proc.stdout:
                await self._ack(line)
        finally:
            feed.cancel()
            with contextlib.suppress(asyncio.CancelledError, ConnectionError):
                await feed
            if proc.returncode is None:
                # EOF lets niks3 finish in-flight pushes.
                proc.stdin.close()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(proc.wait(), STREAM_STOP_GRACE)
            await group.reap()
        logger.warning(
            "uploader exited",
            extra={"uploader": self.name, "returncode": proc.returncode},
        )

    async def run(self) -> None:
        while True:
            try:
                await self._run_child()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("uploader crashed", extra={"uploader": self.name})
            await asyncio.sleep(self.retry_delay)


async def start_uploaders(
    uploaders: Sequence[Uploader], pool: asyncpg.Pool
) -> list[asyncio.Task[None]]:
    await q.drop_unknown_uploaders(pool, names=[u.name for u in uploaders])
    return [asyncio.create_task(u.run(), name=f"uploader-{u.name}") for u in uploaders]
