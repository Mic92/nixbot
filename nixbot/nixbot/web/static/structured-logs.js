// Structured per-derivation log viewer. Cards render lazily from the
// embedded TOC; each fetches its rows (/drv/{idx}) on first open. Rows are
// fixed 20px + content-visibility, so the whole log stays in the DOM
// (native Ctrl-F, anchors, selection, a11y) with off-screen layout skipped;
// the phase bar maps scrollTop -> line via ROW_H. Disclosure is native
// <details>, like the attr-group / error / menu widgets elsewhere.
"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) =>
    s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]);
  const pl = (n, word, suffix = "s") => word + (n === 1 ? "" : suffix);

  const TOC = JSON.parse($("toc-data").textContent);
  const list = $("drv-list");
  const BASE = list.dataset.base;
  const SEARCH = list.dataset.search;
  TOC.forEach((d, i) => (d.pos = i));
  const FAILED = TOC.filter((d) => d.status === "failed");
  const OK = TOC.filter((d) => d.status !== "failed");
  const ROW_H = 20; // must match .log-lines .logline height in style.css

  const phaseBarHTML = `<div class="phasebar" hidden>
    <span class="phase-label"></span>
    <button type="button" class="phase-prev" aria-label="Previous phase">↑ prev</button>
    <button type="button" class="phase-next" aria-label="Next phase">↓ next</button>
  </div>`;

  // ph is [[name, first_line], ...]; wire the bar to the rendered rows
  // in vp. Returns a handle so search can scroll to a line.
  function wireLog(vp, phases, bar) {
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
      bar.querySelector(".phase-label").innerHTML =
        `${esc(phases[cur][0])} phase ` +
        `<span class="phase-pos">${cur + 1}/${phases.length}</span>`;
      bar.querySelector(".phase-prev").disabled = cur <= 0;
      bar.querySelector(".phase-next").disabled = cur >= phases.length - 1;
    }
    if (bar && phases.length) {
      const jump = (dir) => {
        const top = Math.round(vp.scrollTop / ROW_H);
        const t = dir > 0
          ? phases.find((p) => p[1] > top)
          : phases.filter((p) => p[1] < top).pop();
        if (t) scrollToLine(t[1] + 1);
      };
      vp.addEventListener("scroll", update);
      bar.querySelector(".phase-prev").addEventListener(
        "click",
        () => jump(-1),
      );
      bar.querySelector(".phase-next").addEventListener("click", () => jump(1));
      update();
    }
    return { scrollToLine };
  }

  const bodyInnerHTML = (d) =>
    d.n === 0
      ? `<div class="excerpt"><span class="meta">no output</span></div>`
      : `<div class="excerpt">
      <a href="${BASE}/drv/${d.idx}/raw" class="raw">raw&nbsp;↗</a>
    </div>
    ${phaseBarHTML}
    <div class="log-lines" aria-busy="true"></div>`;

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

  const drawn = new WeakMap();
  async function showCard(card) {
    const d = TOC[+card.dataset.pos];
    const body = card.querySelector(".log-card-body");
    if (!body.firstElementChild) body.innerHTML = bodyInnerHTML(d);
    let handle = drawn.get(card);
    if (handle) return handle;
    const vp = body.querySelector(".log-lines");
    if (!vp) return null; // no output: nothing to fetch or wire
    const res = await fetch(`${BASE}/drv/${d.idx}`);
    vp.innerHTML = await res.text();
    vp.removeAttribute("aria-busy");
    handle = wireLog(vp, d.ph, body.querySelector(".phasebar"));
    drawn.set(card, handle);
    return handle;
  }

  // toggle doesn't bubble, so bind per-card when a list is (re)drawn.
  function wireCards(container) {
    for (const c of container.querySelectorAll(".log-card")) {
      c.addEventListener("toggle", () => {
        if (c.open) showCard(c);
      });
    }
  }

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
  const firstFail = list.querySelector(".log-card");
  if (firstFail && FAILED.length) showCard(firstFail);

  const LIST_CAP = 100;
  function drawOk(q) {
    const hits = q ? OK.filter((d) => d.name.toLowerCase().includes(q)) : OK;
    $("succeeded-list").innerHTML = hits
      .slice(0, LIST_CAP)
      .map((d) => cardHTML(d, false))
      .join("");
    wireCards($("succeeded-list"));
  }
  const succeeded = $("succeeded-panel");
  if (succeeded) {
    succeeded.addEventListener("toggle", () => {
      if (succeeded.open && !$("succeeded-list").firstElementChild) drawOk("");
    });
  }

  // Build-scoped search: one debounced request, results grouped by
  // derivation (failures first), each line jumps into the owning card.
  const posByIdx = new Map(TOC.map((d) => [d.idx, d.pos]));
  async function search(raw) {
    const q = raw.trim();
    const results = $("search-results");
    const count = $("search-count");
    results.innerHTML = "";
    if (q.length < 2) {
      count.textContent = "";
      return;
    }
    const res = await fetch(`${SEARCH}?q=${encodeURIComponent(q)}`);
    const groups = (await res.json()).groups;
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
  let t;
  $("search-input").addEventListener("input", (e) => {
    clearTimeout(t);
    const v = e.target.value;
    t = setTimeout(() => search(v), 200);
  });
  $("search-results").addEventListener("click", (e) => {
    const a = e.target.closest("a[data-idx]");
    if (!a) return;
    e.preventDefault();
    openAt(+a.dataset.idx, a.dataset.line ? +a.dataset.line : null);
  });

  async function openAt(idx, line) {
    const pos = posByIdx.get(idx);
    let card = list.querySelector(`.log-card[data-pos="${pos}"]`);
    if (!card && succeeded) {
      succeeded.open = true;
      drawOk(TOC[pos].name.toLowerCase());
      card = list.querySelector(`.log-card[data-pos="${pos}"]`);
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
})();
