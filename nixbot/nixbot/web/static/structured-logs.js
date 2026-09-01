// @ts-check
// Structured per-derivation log viewer. Cards are server-rendered. htmx
// fetches each card's rows (/drv/{idx}) lazily on first open. Phase
// dividers are inline sticky elements the server splices in (see
// phase_sep in logs.py); CSS pins them, so there is no scroll math here.
// Disclosure is native <details>, like the attr-group / error widgets.
"use strict";

/**
 * @typedef {{t?:string,idx:number,status?:string,html?:string,card?:string}} Delta
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
  const LOG_BASE = list.dataset.base; // set only while the build runs

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

  if (LOG_BASE) return runLive();

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

  document.body.addEventListener("htmx:after:settle", (e) => {
    const vp = /** @type {HTMLElement} */ (e.target);
    if (!vp.classList?.contains("log-lines")) return;
    vp.removeAttribute("aria-busy");
    const card = /** @type {HTMLElement|null} */ (vp.closest(".log-card"));
    if (!card) return;
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
    if (pick(card, ".log-lines").hasAttribute("aria-busy")) {
      pending = { card, idx, line }; // afterSwap completes the jump
    } else jump(card, idx, line);
  }

  // Search results are server-rendered. The client only jumps into a
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
    const match = /^#d(\d+)-L(\d+)$/.exec(location.hash);
    if (match) openAt(Number(match[1]), Number(match[2]));
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
    /** @typedef {{el: HTMLDetailsElement, viewport: HTMLElement}} Card */
    /** @type {Map<number, Card>} */
    const cards = new Map();
    /** @param {Element|null} el */
    const processHtmx = (el) =>
      /** @type {any} */ (globalThis).htmx.process(el);
    /** @param {string} s */
    const statusGroup = (s) =>
      s === "failed" || s === "running" ? s : "succeeded";
    const groups = [...list.querySelectorAll(".live-group")].map((group) => ({
      el: /** @type {HTMLElement} */ (group),
      cards: pick(group, ".group-cards"),
    }));

    function updateGroups() {
      for (const group of groups) {
        const n = group.cards.childElementCount;
        group.el.hidden = n === 0;
        for (const count of group.el.querySelectorAll(".count")) {
          count.textContent = `${n}`;
        }
      }
    }

    /** @param {Card} card @param {string} status */
    function setStatus(card, status) {
      const group = statusGroup(status);
      pick(card.el, ".status-icon").className = `status-icon ${group}`;
      card.el.classList.toggle("ok", group !== "failed");
      pick(card.el, ".meta-status").textContent = group === "running"
        ? "building…"
        : status;
      const target = groups.find((g) => g.el.dataset.status === group)?.cards;
      if (target && card.el.parentElement !== target) {
        target.appendChild(card.el);
      }
      updateGroups();
    }

    // Flushed once per frame: a per-line insert + scrollHeight read is a
    // forced layout per line (Mic92/nixbot#98).
    /** @type {Map<number, string[]>} */
    const pendingRows = new Map();
    let flushScheduled = false;
    const MAX_LIVE_ROWS = Number(list.dataset.tail);

    /** Keep the last MAX_LIVE_ROWS; a marker loads the rest on scroll-up.
     * @param {number} idx @param {Card} card */
    function trim(idx, card) {
      const excess = card.viewport.childElementCount - MAX_LIVE_ROWS;
      if (excess <= 0) return;
      const first = card.viewport.children[excess];
      const lineno = /^d\d+-L(\d+)$/.exec(first.id);
      if (!lineno) return;
      const range = document.createRange();
      range.setStartBefore(/** @type {Node} */ (card.viewport.firstChild));
      range.setEndBefore(first);
      range.deleteContents();
      const hidden = Number(lineno[1]) - 1;
      card.viewport.insertAdjacentHTML(
        "afterbegin",
        `<div class="log-elided" role="separator" hx-get="${LOG_BASE}/drv/${idx}?start=0&end=${hidden}" hx-trigger="intersect once" hx-target="this" hx-swap="outerHTML">loading ${
          hidden.toLocaleString("en")
        } hidden lines…</div>`,
      );
    }

    function flush() {
      flushScheduled = false;
      for (const [idx, chunks] of pendingRows) {
        const card = cards.get(idx);
        if (!card) continue;
        // Scrolling up pauses following and trimming.
        const atBottom = card.viewport.scrollHeight - card.viewport.scrollTop -
            card.viewport.clientHeight < 40;
        card.viewport.insertAdjacentHTML("beforeend", chunks.join(""));
        if (atBottom) {
          trim(idx, card);
          if (card.el.open) {
            card.viewport.scrollTop = card.viewport.scrollHeight;
          }
        }
        processHtmx(card.viewport);
      }
      pendingRows.clear();
    }

    /** A state entry or delta: card shell, status change and/or rows.
     * @param {Delta} delta */
    function apply(delta) {
      let card = cards.get(delta.idx);
      if (!card && delta.card) {
        list.insertAdjacentHTML("beforeend", delta.card);
        const el = /** @type {HTMLDetailsElement} */ (list.lastElementChild);
        card = { el, viewport: pick(el, ".log-lines") };
        cards.set(delta.idx, card);
      }
      if (!card) return;
      if (delta.status) {
        flush();
        setStatus(card, delta.status);
        if (delta.t === "status" && delta.status !== "running") {
          card.el.open = delta.status === "failed";
        }
      }
      if (delta.html) {
        const chunks = pendingRows.get(delta.idx);
        if (chunks) chunks.push(delta.html);
        else pendingRows.set(delta.idx, [delta.html]);
        if (!flushScheduled) {
          flushScheduled = true;
          requestAnimationFrame(flush);
        }
      }
    }

    let errors = 0;
    const src = new EventSource(`${LOG_BASE}/stream?format=structured`);
    src.addEventListener("state", (ev) => {
      errors = 0;
      for (const card of cards.values()) card.el.remove();
      cards.clear();
      pendingRows.clear();
      updateGroups();
      for (const delta of JSON.parse(ev.data)) apply(delta);
    });
    src.addEventListener("delta", (ev) => apply(JSON.parse(ev.data)));
    src.addEventListener("done", () => src.close());
    // EventSource reconnects ~every 1s. If the server stays gone (engine
    // restart) give up and reload so the finished log renders server-side.
    src.onerror = () => {
      if (++errors >= 5) {
        src.close();
        setTimeout(() => location.reload(), 3000);
      }
    };
  }
})();
