// @ts-check
// Structured per-derivation log viewer. Cards are server-rendered; htmx
// fetches each card's rows (/drv/{idx}) lazily on first open. Phase
// dividers are inline sticky elements the server splices in (see
// phase_sep in logs.py); CSS pins them, so there is no scroll math here.
// Disclosure is native <details>, like the attr-group / error widgets.
"use strict";

/**
 * @typedef {{idx:number,name:string,status:string,n:number,t0?:number|null,t1?:number|null,html?:string,card?:string}} Drv
 * @typedef {{t:string,idx:number,name?:string,status?:string,from?:number,html?:string,card?:string}} Delta
 */

(() => {
  /** @param {string} id @returns {HTMLElement|null} */
  const $ = (id) => document.getElementById(id);
  /** @param {string} id @returns {HTMLElement} */
  const must = (id) => {
    const el = document.getElementById(id);
    if (!el) throw new Error(`missing #${id}`);
    return el;
  };
  const list = must("drv-list");
  const STREAM = list.dataset.stream; // set only while the build runs

  /** @param {ParentNode} el @param {string} sel @returns {HTMLElement} */
  const pick = (el, sel) => {
    const found = el.querySelector(sel);
    if (!found) throw new Error(`missing ${sel}`);
    return /** @type {HTMLElement} */ (found);
  };

  // Phase nav: prev/next scroll to the sibling divider. The clicked
  // button lives in the sticky (current) divider, so no state to track.
  list.addEventListener("click", (e) => {
    const btn = /** @type {HTMLElement} */ (e.target).closest(
      ".phase-prev, .phase-next",
    );
    if (!btn) return;
    const sep = btn.closest(".phase-sep");
    const vp = /** @type {HTMLElement|null} */ (btn.closest(".log-lines"));
    if (!sep || !vp) return;
    const seps = [...vp.querySelectorAll(".phase-sep")];
    const next = btn.matches(".phase-next");
    const to = seps[seps.indexOf(sep) + (next ? 1 : -1)];
    // Target the phase's first line, not the divider: dividers are sticky
    // (always pinned at top), so nothing scrolls to them. Past the last /
    // before the first phase, fall through to the log's bottom / top.
    const row = to?.nextElementSibling;
    if (row) {
      vp.scrollTop += row.getBoundingClientRect().top -
        vp.getBoundingClientRect().top;
    } else {
      vp.scrollTop = next ? vp.scrollHeight : 0;
    }
  });

  if (STREAM) return runLive();

  // Track cards whose rows are loaded so a jump fires now or waits.
  /** @type {WeakSet<HTMLElement>} */
  const loaded = new WeakSet();
  const succeeded =
    /** @type {HTMLDetailsElement|null} */ ($("succeeded-panel"));
  /** @type {{card:HTMLElement, idx:number, line:number|null}|null} */
  let pending = null;

  /** @param {HTMLElement} card @param {number} idx @param {number|null} line */
  function jump(card, idx, line) {
    card.scrollIntoView({ block: "nearest" });
    document
      .querySelectorAll(".log-lines .logline.hit")
      .forEach((x) => x.classList.remove("hit"));
    if (line == null) return;
    const el = document.getElementById(`d${idx}-L${line}`);
    el?.classList.add("hit");
    el?.scrollIntoView({ block: "center" });
  }

  document.body.addEventListener("htmx:afterSwap", (e) => {
    const vp = /** @type {HTMLElement} */ (e.target);
    if (!vp.classList?.contains("log-lines")) return;
    vp.removeAttribute("aria-busy");
    const card = /** @type {HTMLElement|null} */ (vp.closest(".log-card"));
    if (!card) return;
    loaded.add(card);
    if (pending && pending.card === card) {
      jump(card, pending.idx, pending.line);
      pending = null;
    } else if (!card.classList.contains("ok")) {
      vp.scrollTop = vp.scrollHeight; // a failure's error is at the end
    }
  });

  // Open the owning card (loading its rows if needed) and jump to a line.
  /** @param {number} idx @param {number|null} line */
  function openAt(idx, line) {
    const card = /** @type {HTMLDetailsElement|null} */ (
      list.querySelector(`.log-card[data-idx="${idx}"]`)
    );
    if (!card) return;
    if (succeeded && succeeded.contains(card)) succeeded.open = true;
    card.open = true; // triggers the htmx fetch if not yet loaded
    if (loaded.has(card)) jump(card, idx, line);
    else pending = { card, idx, line }; // afterSwap completes the jump
  }

  // Search results are server-rendered; the client only jumps into a
  // card, waiting for htmx to load its rows when they aren't yet present.
  must("search-results").addEventListener("click", (e) => {
    const a = /** @type {HTMLElement} */ (e.target).closest("a[data-idx]");
    if (!a) return;
    e.preventDefault();
    const link = /** @type {HTMLElement} */ (a);
    openAt(
      Number(link.dataset.idx),
      link.dataset.line ? Number(link.dataset.line) : null,
    );
  });

  // A #d{idx}-L{n} permalink can't scroll on its own: the row is fetched
  // lazily, so on load (and on hashchange) resolve it through openAt.
  function jumpToHash() {
    const m = /^#d(\d+)-L(\d+)$/.exec(location.hash);
    if (m) openAt(Number(m[1]), Number(m[2]));
  }
  globalThis.addEventListener("hashchange", jumpToHash);
  // Wait for htmx to wire its toggle triggers (on DOMContentLoaded);
  // opening a card sooner fires toggle into the void and never fetches.
  if (document.readyState === "complete") jumpToHash();
  else {document.addEventListener("DOMContentLoaded", jumpToHash, {
      once: true,
    });}

  // Live mode: build cards from the structured SSE (state burst + deltas),
  // in arrival order, the running one following its tail. The
  // terminal-status reload swaps to the polished container page.
  function runLive() {
    /** @type {Map<number, Drv>} */
    const byIdx = new Map();
    /** @type {Map<number, HTMLDetailsElement>} */
    const cardOf = new Map();
    /** @param {string} s */
    const iconState = (s) =>
      s === "failed" ? "failed" : s === "running" ? "running" : "succeeded";

    /** Insert the server-rendered card shell (same drv_card macro as the
     * finished page).
     * @param {Drv} d @returns {HTMLDetailsElement} */
    function ensureCard(d) {
      const existing = cardOf.get(d.idx);
      if (existing) return existing;
      list.insertAdjacentHTML("beforeend", d.card ?? "");
      const el = /** @type {HTMLDetailsElement} */ (list.lastElementChild);
      cardOf.set(d.idx, el);
      return el;
    }

    /** @param {Drv} d @returns {HTMLDetailsElement} */
    function setMeta(d) {
      const el = ensureCard(d);
      pick(el, ".status-icon").className = "status-icon " + iconState(d.status);
      el.classList.toggle("ok", d.status !== "failed");
      pick(el, ".meta").textContent = d.status === "running"
        ? "building…"
        : d.status;
      return el;
    }

    /** Append server-rendered rows (ANSI already applied).
     * @param {number} idx @param {number} n @param {string} html */
    function addLines(idx, n, html) {
      const d = byIdx.get(idx);
      if (!d || !html) return;
      const el = ensureCard(d);
      const vp = pick(el, ".log-lines");
      // Follow the tail only when already at the bottom, so scrolling up
      // to read pauses following and scrolling back resumes it.
      const atBottom = vp.scrollHeight - vp.scrollTop - vp.clientHeight < 40;
      vp.insertAdjacentHTML("beforeend", html);
      d.n = n;
      if (el.open && atBottom) vp.scrollTop = vp.scrollHeight;
    }

    /** @param {Delta} delta */
    function apply(delta) {
      const d = byIdx.get(delta.idx);
      if (delta.t === "drv") {
        const nd = {
          idx: delta.idx,
          name: delta.name ?? "",
          status: "running",
          n: 0,
          card: delta.card,
        };
        byIdx.set(nd.idx, nd);
        setMeta(nd);
      } else if (delta.t === "line") {
        addLines(delta.idx, delta.from ?? 1, delta.html ?? "");
      } else if (delta.t === "phase" && d) {
        // The divider is a normal row appended before the phase's output.
        pick(ensureCard(d), ".log-lines").insertAdjacentHTML(
          "beforeend",
          delta.html ?? "",
        );
      } else if (delta.t === "status" && d) {
        d.status = delta.status ?? d.status;
        const el = setMeta(d);
        if (delta.status === "failed") el.open = true;
        else if (delta.status !== "running") el.open = false;
      }
    }

    /** @param {Drv[]} state */
    function reset(state) {
      list.innerHTML = "";
      byIdx.clear();
      cardOf.clear();
      for (const e of state) {
        const d = {
          idx: e.idx,
          name: e.name,
          status: e.status,
          n: e.n,
          card: e.card,
        };
        byIdx.set(d.idx, d);
        setMeta(d);
        addLines(d.idx, e.n, e.html || "");
      }
    }

    let errors = 0;
    const src = new EventSource(/** @type {string} */ (STREAM));
    src.addEventListener("state", (ev) => {
      errors = 0;
      reset(JSON.parse(ev.data));
    });
    src.addEventListener("delta", (ev) => apply(JSON.parse(ev.data)));
    src.addEventListener("done", () => src.close());
    // EventSource reconnects ~every 1s; if the server stays gone (engine
    // restart) give up and reload so the finished log renders server-side.
    src.onerror = () => {
      if (++errors >= 5) {
        src.close();
        setTimeout(() => location.reload(), 3000);
      }
    };
  }
})();
