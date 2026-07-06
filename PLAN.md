# New log viewer — implementation plan

Per-derivation build logs: structured storage, phase tracking, global search,
`content-visibility` rendering. Prototypes: `~/.claude/outputs/logstore/`
(storage + search) and `~/.claude/outputs/logviz-mockups/scale.html` (frontend).

## Reality of the current pipeline

`nix build {drv}^*` runs **one invocation per attribute**; logs are keyed per
`(build_id, attr)`, one zstd file each, streamed independently. The "250
derivations" of the mockup are **attributes** — the build page already lists
them from the DB (`queries.attributes`), each linking to its own log.

Within one attribute's invocation the log may still contain several derivations
(the attr's own drv plus any uncached deps), interleaved by `internal-json`
activity id — but the common case is a single derivation with phases. So
structure lives _inside_ the per-attr log; there is no per-build container.

## Goals

- Structured per-attr log: per-derivation frames, phases, timings; existing
  `(build, attr)` keying and independent streaming unchanged.
- Build page stays the DB attribute list; each attr expands to its log.
- Viewer scales to 250+ attributes / large logs: whole-log DOM with
  `content-visibility` (native find/anchors kept), lazy card expansion, global
  search; manual virtualization only for monster logs.
- No eager migration: new format for new builds, dual-read for old.

Not now: cross-build search index, backfill, changes to raw/grep endpoints.

## Storage format (adapted from the prototype)

Per-`(build, attr)` file, replacing the current flat `.zst`:
`<frames><TOC json><u32 len><NBL1>`.

- One frame per derivation (grouped only when an invocation builds several small
  deps), zstd level 12. Ratio (~11x per-attr) is not a design driver; logs are
  small. L19 costs 12x the CPU for ~18% ratio; trained dicts hurt per-frame
  compression — both unused.
- TOC entry per derivation:
  ```jsonc
  {
    "name": "...", "status": "built|failed",
    "off": <frame offset>, "clen": <frame len>,
    "bs": <byte start in frame>, "bn": <byte len>, "n": <lines>,
    "ph": [["build", <first line>], ...],
    "t0": <start ms>, "t1": <stop ms>
  }
  ```
- Read a derivation: decompress its frame (cached), slice `[bs:bs+bn]`,
  `splitlines`. Slice by byte range rather than splitting the whole frame (3x
  faster reads).
- Monster drvs (>4 MB text): split into multiple frames + sub-line index. The 64
  MB head+tail cap still applies per log; the viewer must show a truncation gap.
- Format optimizes write CPU and read latency, not ratio.

## Phase 1 — Storage library

`nixbot/nixbot/logstore.py` (port prototype):

- `LogContainerWriter`: `(drv, phase, line, ts)` records → per-group buffers →
  64 KB frames + TOC.
- `LogContainerReader`: tail-parse TOC; `drv(i)`, `phases(i)`, `drv_all()`.
- `search(reader, query, per_drv_cap)`: frame fast-reject + position-locate
  - byte-bisect attribution. Scoped to one log, no index.

Tests (`tests/test_logstore.py`, TDD): byte-identical round-trip; TOC phase
indices / line counts / byte ranges; search across rare/common/phrase terms with
line-number accuracy; write/read latency guards.

## Phase 2 — Ingest: capture structure

`nixbot/nixbot/executor.py`:

- `render_log_event`: add `SetPhase` (result type 104) per activity id, and
  activity `stop` timestamps for durations.
- Key frames by full drv path (`ACT_BUILD` `fields[0]`), not the truncated
  display name — fixes collisions when an invocation builds several drvs.
- `_pump_output` / `LogWriter`: within each attr's existing stream, demux by
  activity id into per-derivation buffers feeding `LogContainerWriter`;
  unprefixed nix output → a synthetic `driver` frame.

Tests: `internal-json` fixtures → assert per-derivation grouping, phase ranges,
driver bucket. Keep transient-error detection and `failure_excerpt` working off
the demuxed stream.

## Phase 3 — Web

`nixbot/nixbot/web/logs.py`:

- Format dispatch in `_resolve`: `NBL1` container → serve from it; else the
  legacy flat renderer. Runs the whole mixed-format retention window.
- Endpoints extend the existing per-attr routes (`.../builds/{n}/logs/{attr}`):
  - `.../logs/{attr}` — TOC (derivations + phases + timings) for the shell.
  - `.../logs/{attr}/lines` — a derivation's text; whole log by default,
    `?from=&to=` only for monster logs past the render cap.
  - `.../builds/{n}/search?q=` — build-scoped global search across the build's
    per-attr containers; returns per-derivation hit groups.
- Raw text: project the log from frames in timestamp order; legacy raw route
  unchanged.
- SSE unchanged (still per attr); demux happens on the write side.

## Phase 4 — Frontend

Port `scale.html` into nixbot templates/static:

- Build page: attribute list from the DB, failures first, successes behind a
  count + expand-to-browse. Each row is a unified card (disclosure → inline
  log). Reuse nixbot tokens.
- Rendering: whole log as real DOM rows with `content-visibility: auto` (fixed
  20 px rows). Native Ctrl-F, `#L` anchors, selection and screen readers all
  work; the browser skips off-screen layout. Manual virtualization +
  `?from=&to=` fetch only past a large line/byte cap (monster logs), with the
  raw log as the accessible path there.
- Phase bar from the container TOC `ph`; hidden when empty (legacy logs).
- Search: one global box (names + log content), results grouped by derivation,
  failures first, keyboard-navigable; a hit jumps to the card + line. No per-log
  find UI — within a log is native Ctrl-F.
- Touch: 44 px targets under `pointer: coarse`, 14 rem log cap on phones.
- A11y: semantic disclosures, ARIA, native find/anchors from real DOM rows.

## Phase 5 — Migration

- No backfill, no eager conversion. Old builds serve via dual-read; retention
  retires them over one window.
- UI degrades on legacy builds: no phase bar, no durations, log + find still
  work.
- Flag the new writer; roll new builds on, watch ingest CPU and sizes, remove
  the flag.

## Sequencing & risk

1. Phase 1: self-contained, offline-testable — start here.
2. Phase 2: riskiest (rewrites the hot output path). Flag it, keep the legacy
   writer until parity is proven on real builds.
3. Phases 3–4: build against Phase 1 with synthetic containers before Phase 2
   lands.
4. Watch: byte-accurate demux when several drvs interleave in one invocation;
   the `driver` bucket catching all unprefixed output; monster-drv sub-framing.

## Deferred

- Cross-build search: per-log trigram/bloom in the TOC to pick candidate attrs,
  then scan; or real FTS.
- Timeline view (timings now captured).
- zstd long-distance matching for monster drvs.
