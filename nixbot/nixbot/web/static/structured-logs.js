// @ts-check
// Structured per-derivation log viewer. Cards render lazily from the
// embedded TOC; each fetches its rows (/drv/{idx}) on first open. Rows are
// fixed 20px + content-visibility, so the whole log stays in the DOM
// (native Ctrl-F, anchors, selection, a11y) with off-screen layout skipped;
// the phase bar maps scrollTop -> line via ROW_H. Disclosure is native
// <details>, like the attr-group / error / menu widgets elsewhere.
"use strict";

/**
 * @typedef {{idx:number,name:string,status:string,ph:[string,number][],n:number,pos?:number,t0?:number|null,t1?:number|null,lines?:string[]}} Drv
 * @typedef {{t:string,idx:number,name?:string,status?:string,phase?:string,line?:number,from?:number,text?:string}} Delta
 * @typedef {{idx:number,name:string,status:string,lines:number[]}} Group
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
  const escMap = /** @type {Record<string,string>} */ (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;" }
  );
  /** @param {string} s @returns {string} */
  const esc = (s) => s.replace(/[&<>]/g, (c) => escMap[c]);
  /** @param {number} n @param {string} word @param {string} [suffix] */
  const pl = (n, word, suffix = "s") => word + (n === 1 ? "" : suffix);

  const list = must("drv-list");
  const BASE = list.dataset.base;
  const SEARCH = list.dataset.search;
  const STREAM = list.dataset.stream; // set only while the build runs
  const ROW_H = 20; // must match .log-lines .logline height in style.css
  /** @param {string} s */
  // deno-lint-ignore no-control-regex -- ESC is the ANSI sequence intro
  const stripAnsi = (s) => s.replace(/\x1b\[[0-9;]*m/g, "");

  const phaseBarHTML = `<div class="phasebar" hidden>
    <span class="phase-label"></span>
    <button type="button" class="phase-prev" aria-label="Previous phase">↑ prev</button>
    <button type="button" class="phase-next" aria-label="Next phase">↓ next</button>
  </div>`;

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
      for (const [, start] of phases) {
        if (start <= top) cur++;
        else break;
      }
      bar.hidden = cur < 0;
      if (cur < 0) return;
      pick(bar, ".phase-label").innerHTML = `${esc(phases[cur][0])} phase ` +
        `<span class="phase-pos">${cur + 1}/${phases.length}</span>`;
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

  /** @param {Drv} d */
  const bodyInnerHTML = (d) =>
    d.n === 0
      ? `<div class="excerpt"><span class="meta">no output</span></div>`
      : `<div class="excerpt">
      <a href="${BASE}/drv/${d.idx}/raw" class="raw">raw&nbsp;↗</a>
    </div>
    ${phaseBarHTML}
    <div class="log-lines" aria-busy="true"></div>`;

  /** @param {Drv} d @param {boolean} open */
  function cardHTML(d, open) {
    const failed = d.status === "failed";
    const dur = d.t0 != null && d.t1 != null
      ? `${((d.t1 - d.t0) / 1000).toFixed(1)}s`
      : "";
    const meta = failed ? "failed" : ["built", dur].filter(Boolean).join(" · ");
    const state = failed ? "failed" : "succeeded";
    return `<details class="log-card${
      failed ? "" : " ok"
    }" data-pos="${d.pos}"${open ? " open" : ""}>
      <summary>
        <span class="status-icon ${state}" aria-hidden="true"></span>
        <span class="card-text">
          <span class="card-name">${esc(d.name)}</span>
          <span class="meta">${meta}</span>
        </span>
      </summary>
      <div class="log-card-body">${open ? bodyInnerHTML(d) : ""}</div>
    </details>`;
  }

  /** @type {WeakMap<HTMLElement, LogHandle>} */
  const drawn = new WeakMap();
  /** @param {HTMLElement} card @returns {Promise<LogHandle|null>} */
  async function showCard(card) {
    const d = TOC[Number(card.dataset.pos)];
    const body = pick(card, ".log-card-body");
    if (!body.firstElementChild) body.innerHTML = bodyInnerHTML(d);
    const cached = drawn.get(card);
    if (cached) return cached;
    const vp =
      /** @type {HTMLElement|null} */ (body.querySelector(".log-lines"));
    if (!vp) return null; // no output: nothing to fetch or wire
    const res = await fetch(`${BASE}/drv/${d.idx}`);
    vp.innerHTML = await res.text();
    vp.removeAttribute("aria-busy");
    const handle = wireLog(
      vp,
      d.ph,
      /** @type {HTMLElement|null} */ (body.querySelector(".phasebar")),
    );
    drawn.set(card, handle);
    return handle;
  }

  // toggle doesn't bubble, so bind per-card when a list is (re)drawn.
  /** @param {ParentNode} container */
  function wireCards(container) {
    for (const c of container.querySelectorAll(".log-card")) {
      const card = /** @type {HTMLDetailsElement} */ (c);
      card.addEventListener("toggle", () => {
        if (card.open) showCard(card);
      });
    }
  }

  if (STREAM) return runLive();

  const TOC = /** @type {Drv[]} */ (
    JSON.parse(must("toc-data").textContent ?? "[]")
  );
  TOC.forEach((d, i) => (d.pos = i));
  const FAILED = TOC.filter((d) => d.status === "failed");
  const OK = TOC.filter((d) => d.status !== "failed");

  // failures first (first one open); successes collapsed behind a card.
  const okBlock = OK.length
    ? `<h2 class="section">Succeeded (${OK.length})</h2>
       <details class="succeeded-panel" id="succeeded-panel">
         <summary>
           <span class="status-icon succeeded" aria-hidden="true"></span>
           <span class="card-text"><span class="card-name">${OK.length} built</span>
             <span class="meta">expand to browse</span></span>
         </summary>
         <div id="succeeded-list"></div>
       </details>`
    : "";
  list.innerHTML =
    (FAILED.length
      ? `<h2 class="section">Failures (${FAILED.length})</h2>`
      : "") +
    FAILED.map((d, i) => cardHTML(d, i === 0)).join("") +
    okBlock;
  wireCards(list);
  const firstFail = /** @type {HTMLElement|null} */ (
    list.querySelector(".log-card")
  );
  if (firstFail && FAILED.length) showCard(firstFail);

  const LIST_CAP = 100;
  /** @param {string} q */
  function drawOk(q) {
    const hits = q ? OK.filter((d) => d.name.toLowerCase().includes(q)) : OK;
    must("succeeded-list").innerHTML = hits
      .slice(0, LIST_CAP)
      .map((d) => cardHTML(d, false))
      .join("");
    wireCards(must("succeeded-list"));
  }
  const succeeded =
    /** @type {HTMLDetailsElement|null} */ ($("succeeded-panel"));
  if (succeeded) {
    succeeded.addEventListener("toggle", () => {
      if (succeeded.open && !must("succeeded-list").firstElementChild) {
        drawOk("");
      }
    });
  }

  // Build-scoped search: one debounced request, results grouped by
  // derivation (failures first), each line jumps into the owning card.
  const posByIdx = new Map(
    TOC.map((d) => [d.idx, /** @type {number} */ (d.pos)]),
  );
  /** @param {string} raw */
  async function search(raw) {
    const q = raw.trim();
    const results = must("search-results");
    const count = must("search-count");
    results.innerHTML = "";
    if (q.length < 2) {
      count.textContent = "";
      return;
    }
    const res = await fetch(`${SEARCH}?q=${encodeURIComponent(q)}`);
    const groups = /** @type {Group[]} */ ((await res.json()).groups);
    const total = groups.reduce((n, g) => n + g.lines.length, 0);
    count.textContent = groups.length
      ? `${groups.length} ${pl(groups.length, "derivation")}, ` +
        `${total} log ${pl(total, "match", "es")}`
      : "no matches";
    results.innerHTML = groups
      .map((g) => {
        const ok = g.status !== "failed";
        const head =
          `<a href="#" class="search-drv${
            ok ? " ok" : ""
          }" data-idx="${g.idx}">` +
          `<span class="status-icon ${
            ok ? "succeeded" : "failed"
          }" aria-hidden="true"></span>` +
          `<span class="search-name">${esc(g.name)}</span>` +
          `<span class="search-badge">${g.lines.length} ${
            pl(g.lines.length, "match", "es")
          }</span></a>`;
        const lines = g.lines
          .map(
            (ln) =>
              `<li><a href="#" data-idx="${g.idx}" data-line="${ln}">` +
              `<span class="search-line">${ln}</span></a></li>`,
          )
          .join("");
        return `<li class="search-group">${head}<ul class="search-lines">${lines}</ul></li>`;
      })
      .join("");
  }
  let t = 0;
  must("search-input").addEventListener("input", (e) => {
    clearTimeout(t);
    const v = /** @type {HTMLInputElement} */ (e.target).value;
    t = setTimeout(() => search(v), 200);
  });
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

  /** @param {number} idx @param {number|null} line */
  async function openAt(idx, line) {
    const pos = posByIdx.get(idx);
    if (pos == null) return;
    let card = /** @type {HTMLDetailsElement|null} */ (
      list.querySelector(`.log-card[data-pos="${pos}"]`)
    );
    if (!card && succeeded) {
      succeeded.open = true;
      drawOk(TOC[pos].name.toLowerCase());
      card = /** @type {HTMLDetailsElement|null} */ (
        list.querySelector(`.log-card[data-pos="${pos}"]`)
      );
    }
    if (!card) return;
    card.open = true;
    const handle = await showCard(card);
    card.scrollIntoView({ block: "nearest" });
    document
      .querySelectorAll(".log-lines .logline.hit")
      .forEach((x) => x.classList.remove("hit"));
    if (line == null || !handle) return;
    const row = document.getElementById(`d${idx}-L${line}`);
    if (row) row.classList.add("hit");
    handle.scrollToLine(line);
  }

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
    /** @param {number} idx @param {number} n @param {string} text */
    const rowHTML = (idx, n, text) =>
      `<span class="logline" id="d${idx}-L${n}">` +
      `<a class="lineno" href="#d${idx}-L${n}">${n}</a>${
        esc(stripAnsi(text))
      }</span>`;

    /** @param {Drv} d @returns {HTMLDetailsElement} */
    function cardEl(d) {
      const el = document.createElement("details");
      el.className = "log-card" + (d.status === "failed" ? "" : " ok");
      el.dataset.idx = String(d.idx);
      if (d.status === "running" || d.status === "failed") el.open = true;
      el.innerHTML = `<summary>
          <span class="status-icon ${
        iconState(d.status)
      }" aria-hidden="true"></span>
          <span class="card-text"><span class="card-name">${
        esc(d.name)
      }</span><span class="meta"></span></span>
        </summary>
        <div class="log-card-body"><div class="excerpt"></div>${phaseBarHTML}<div class="log-lines"></div></div>`;
      return el;
    }

    /** @param {Drv} d @returns {HTMLDetailsElement} */
    function ensureCard(d) {
      const existing = cardOf.get(d.idx);
      if (existing) return existing;
      const el = cardEl(d);
      list.appendChild(el);
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

    /** @param {number} idx @param {number} from @param {string[]} texts */
    function addLines(idx, from, texts) {
      const d = byIdx.get(idx);
      if (!d || !texts.length) return;
      const el = ensureCard(d);
      const vp = pick(el, ".log-lines");
      // Follow the tail only when already at the bottom, so scrolling up
      // to read pauses following and scrolling back resumes it.
      const atBottom = vp.scrollHeight - vp.scrollTop - vp.clientHeight < 40;
      vp.insertAdjacentHTML(
        "beforeend",
        texts.map((t, k) => rowHTML(idx, from + k, t)).join(""),
      );
      d.n = from + texts.length - 1;
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
        };
        byIdx.set(nd.idx, nd);
        setMeta(nd);
      } else if (delta.t === "line") {
        addLines(delta.idx, delta.from ?? 1, [delta.text ?? ""]);
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
        };
        byIdx.set(d.idx, d);
        setMeta(d);
        addLines(d.idx, 1, e.lines || []);
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
