"""Tests for the per-derivation log container (round-trip, TOC, search)."""

# ruff: noqa: PLR2004 (test literals)

from __future__ import annotations

from nixbot.logstore import (
    LogContainerReader,
    LogContainerWriter,
    is_container,
)


def _write(drvs: list[tuple[str, list[str]]], **kw: int) -> bytes:
    w = LogContainerWriter(**kw)
    for name, lines in drvs:
        for i, text in enumerate(lines):
            w.line(name, text, ts=i)
    return w.finalize()


def test_roundtrip_identical() -> None:
    drvs = [
        ("qtbase", ["configure", "CC main.o", "error: boom"]),
        ("zlib", ["unpacking", "installing"]),
    ]
    r = LogContainerReader(_write(drvs))
    assert len(r) == 2
    assert r.lines(0) == drvs[0][1]
    assert r.lines(1) == drvs[1][1]


def test_is_container() -> None:
    blob = _write([("a", ["x"])])
    assert is_container(blob)
    assert not is_container(b"plain text log")
    assert not is_container(b"")


def test_toc_metadata() -> None:
    w = LogContainerWriter()
    for i, t in enumerate(["a", "b", "c", "d"]):
        w.line("pkg", t, ts=i * 10)
    w.status("pkg", "failed")
    r = LogContainerReader(w.finalize())
    e = r.entry(0)
    assert e["name"] == "pkg"
    assert e["status"] == "failed"
    assert e["n"] == 4
    assert e["t0"] == 0
    assert e["t1"] == 30


def test_phase_indices() -> None:
    w = LogContainerWriter()
    w.phase("pkg", "unpack")
    w.line("pkg", "unpacking source")
    w.phase("pkg", "build")
    w.line("pkg", "CC a.o")
    w.line("pkg", "CC b.o")
    w.phase("pkg", "install")
    w.line("pkg", "installing")
    r = LogContainerReader(w.finalize())
    assert r.entry(0)["ph"] == [["unpack", 0], ["build", 1], ["install", 3]]


def test_phase_dedup() -> None:
    w = LogContainerWriter()
    w.phase("p", "build")
    w.line("p", "x")
    w.phase("p", "build")  # same phase repeated -> no new marker
    w.line("p", "y")
    r = LogContainerReader(w.finalize())
    assert r.entry(0)["ph"] == [["build", 0]]


def test_grouping_shares_frame() -> None:
    # Small drvs group into one frame; a big one forces a new frame.
    w = LogContainerWriter(group_bytes=64)
    w.line("a", "x" * 10)
    w.line("b", "y" * 10)
    w.line("c", "z" * 100)  # pushes past 64 bytes -> flush
    w.line("d", "w" * 5)
    r = LogContainerReader(w.finalize())
    offs = [r.entry(i)["off"] for i in range(4)]
    assert offs[0] == offs[1]  # a, b share the first frame
    assert offs[3] != offs[0]  # d lands in a later frame
    assert r.lines(0) == ["x" * 10]
    assert r.lines(2) == ["z" * 100]


def test_search_rare_term() -> None:
    drvs = [
        ("qtbase", ["configure", "CC a.o", "error: qtbase_init undeclared"]),
        ("zlib", ["configure", "CC z.o", "done"]),
    ]
    r = LogContainerReader(_write(drvs))
    hits = r.search("undeclared")
    assert len(hits) == 1
    assert hits[0]["name"] == "qtbase"
    assert hits[0]["lines"] == [3]


def test_search_common_term_line_numbers() -> None:
    drvs = [
        ("a", ["CC 1", "ld", "CC 2"]),
        ("b", ["CC 3", "done"]),
    ]
    r = LogContainerReader(_write(drvs))
    hits = {h["name"]: h["lines"] for h in r.search("cc")}
    assert hits == {"a": [1, 3], "b": [1]}


def test_search_phrase() -> None:
    r = LogContainerReader(_write([("a", ["exit code 1", "exit code 2"])]))
    hits = r.search("exit code 1")
    assert hits[0]["lines"] == [1]


def test_search_per_drv_cap() -> None:
    r = LogContainerReader(_write([("a", ["hit"] * 50)]))
    hits = r.search("hit", per_drv_cap=10)
    assert len(hits[0]["lines"]) == 10


def test_search_across_grouped_frame() -> None:
    # Two drvs in one frame; matches must attribute to the right drv/line.
    w = LogContainerWriter(group_bytes=1 << 20)
    w.line("a", "alpha")
    w.line("a", "target here")
    w.line("b", "beta")
    w.line("b", "target too")
    r = LogContainerReader(w.finalize())
    assert r.entry(0)["off"] == r.entry(1)["off"]  # same frame
    hits = {h["name"]: h["lines"] for h in r.search("target")}
    assert hits == {"a": [2], "b": [2]}
