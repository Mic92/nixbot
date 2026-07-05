// @ts-check
// Structured per-derivation log viewer. Cards are server-rendered; htmx
// fetches each card's rows (/drv/{idx}) lazily on first open. Rows are
// fixed 20px + content-visibility, so the whole log stays in the DOM
// (native Ctrl-F, anchors, selection, a11y) with off-screen layout skipped;
// the phase bar maps scrollTop -> line via ROW_H. Disclosure is native
// <details>, like the attr-group / error / menu widgets elsewhere.
"use strict";

/**
 * @typedef {{idx:number,name:string,status:string,ph:[string,number][],n:number,t0?:number|null,t1?:number|null,html?:string,card?:string}} Drv
 * @typedef {{t:string,idx:number,name?:string,status?:string,phase?:string,line?:number,from?:number,html?:string,card?:string}} Delta
 * @typedef {{scrollToLine:(n:number)=>void,refresh:()=>void}} LogHandle
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
  /** @param {string} s @returns {string} */
  const esc = (s) =>
    s.replace(
      /[&<>]/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c] ?? c),
    );

  const list = must("drv-list");
  const STREAM = list.dataset.stream; // set only while the build runs
  const ROW_H = 20; // must match .log-lines .logline height in style.css

  /** @param {ParentNode} el @param {string} sel @returns {HTMLElement} */
  const pick = (el, sel) => {
    const found = el.querySelector(sel);
    if (!found) throw new Error(`missing ${sel}`);
    return /** @type {HTMLElement} */ (found);
  };

  // ph is [[name, first_line], ...]; wire the bar to the rendered rows
  // in vp. Returns a handle so search can scroll to a line.
  /**
   * @param {HTMLElement} vp
   * @param {[string,number][]} phases
   * @param {HTMLElement|null} bar
   * @returns {LogHandle}
   */
  function wireLog(vp, phases, bar) {
    /** @param {number} n */
    const scrollToLine = (n) => {
      vp.scrollTop = Math.max(0, (n - 1) * ROW_H - vp.clientHeight / 2);
      update();
    };
    function update() {
      if (!bar || !phases.length) return;
      const top = Math.round(vp.scrollTop / ROW_H);
      let cur = -1;
      // first_line is 1-based, top 0-based: first phase shows at top=0.
      for (const [, start] of phases) {
        if (start - 1 <= top) cur++;
        else break;
      }
      bar.hidden = cur < 0;
      if (cur < 0) return;
      pick(bar, ".phase-label").innerHTML = `${esc(phases[cur][0])} ` +
        `<span class="phase-pos">(${cur + 1}/${phases.length})</span>`;
      /** @type {HTMLButtonElement} */ (pick(bar, ".phase-prev")).disabled =
        cur <= 0;
      /** @type {HTMLButtonElement} */ (pick(bar, ".phase-next")).disabled =
        cur >= phases.length - 1;
    }
    if (bar && phases.length) {
      /** @param {number} dir */
      const jump = (dir) => {
        const top = Math.round(vp.scrollTop / ROW_H);
        const t = dir > 0
          ? phases.find((p) => p[1] > top)
          : phases.filter((p) => p[1] < top).pop();
        if (t) scrollToLine(t[1] + 1);
      };
      vp.addEventListener("scroll", update);
      pick(bar, ".phase-prev").addEventListener("click", () => jump(-1));
      pick(bar, ".phase-next").addEventListener("click", () => jump(1));
      update();
    }
    return { scrollToLine, refresh: update };
  }

  if (STREAM) return runLive();

  // htmx fetches each card's rows into .log-lines (on open, or on load for
  // the first failure); here we wire the phase bar to the swapped-in rows.
  /** @type {WeakMap<HTMLElement, LogHandle>} */
  const drawn = new WeakMap();
  const succeeded =
    /** @type {HTMLDetailsElement|null} */ ($("succeeded-panel"));
  /** @type {{card:HTMLElement, idx:number, line:number|null}|null} */
  let pending = null;

  /** @param {HTMLElement} card @param {LogHandle} handle
   * @param {number} idx @param {number|null} line */
  function jump(card, handle, idx, line) {
    card.scrollIntoView({ block: "nearest" });
    document
      .querySelectorAll(".log-lines .logline.hit")
      .forEach((x) => x.classList.remove("hit"));
    if (line == null) return;
    document.getElementById(`d${idx}-L${line}`)?.classList.add("hit");
    handle.scrollToLine(line);
  }

  document.body.addEventListener("htmx:afterSwap", (e) => {
    const vp = /** @type {HTMLElement} */ (e.target);
    if (!vp.classList?.contains("log-lines")) return;
    vp.removeAttribute("aria-busy");
    const card = /** @type {HTMLElement|null} */ (vp.closest(".log-card"));
    if (!card) return;
    // A capped (head+tail) log has a gap, so scrollTop->line math and its
    // phase bar would be wrong; drop the phases for those.
    const ph = /** @type {[string,number][]} */ (
      vp.querySelector(".log-elided") ? [] : JSON.parse(card.dataset.ph || "[]")
    );
    const handle = wireLog(vp, ph, card.querySelector(".phasebar"));
    drawn.set(card, handle);
    if (pending && pending.card === card) {
      jump(card, handle, pending.idx, pending.line);
      pending = null;
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
    const handle = drawn.get(card);
    if (handle) jump(card, handle, idx, line);
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
    /** @type {Map<number, LogHandle>} */
    const handleOf = new Map();
    /** @param {string} s */
    const iconState = (s) =>
      s === "failed" ? "failed" : s === "running" ? "running" : "succeeded";

    /** Insert the server-rendered card shell (same drv_card macro as the
     * finished page) and wire its phase bar.
     * @param {Drv} d @returns {HTMLDetailsElement} */
    function ensureCard(d) {
      const existing = cardOf.get(d.idx);
      if (existing) return existing;
      list.insertAdjacentHTML("beforeend", d.card ?? "");
      const el = /** @type {HTMLDetailsElement} */ (list.lastElementChild);
      cardOf.set(d.idx, el);
      handleOf.set(
        d.idx,
        wireLog(pick(el, ".log-lines"), d.ph, el.querySelector(".phasebar")),
      );
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
          /** @type {[string,number][]} */ ph: [],
          n: 0,
          card: delta.card,
        };
        byIdx.set(nd.idx, nd);
        setMeta(nd);
      } else if (delta.t === "line") {
        addLines(delta.idx, delta.from ?? 1, delta.html ?? "");
      } else if (delta.t === "phase" && d) {
        if (!d.ph.length || d.ph[d.ph.length - 1][0] !== delta.phase) {
          d.ph.push([delta.phase ?? "", delta.line ?? 0]);
        }
        handleOf.get(d.idx)?.refresh();
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
      handleOf.clear();
      for (const e of state) {
        const d = {
          idx: e.idx,
          name: e.name,
          status: e.status,
          ph: e.ph || [],
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
