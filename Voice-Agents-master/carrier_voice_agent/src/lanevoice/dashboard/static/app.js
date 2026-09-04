/* LaneVoice dashboard — no framework, no external requests.
   All dynamic text goes through textContent (el() below): transcripts, notes
   and carrier names are caller-influenced data, never markup. */
"use strict";

/* ------------------------------------------------------------ dom helpers */
const $ = (sel) => document.querySelector(sel);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on") && typeof v === "function") {
      node.addEventListener(k.slice(2), v);
    } else if (k === "dataset") Object.assign(node.dataset, v);
    else node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

const SVG_NS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const child of children.flat()) if (child) node.append(child);
  return node;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON error page */ }
  if (!res.ok) {
    throw new Error((data && data.error) || `${res.status} ${res.statusText}`);
  }
  return data;
}

/* ------------------------------------------------------------- formatting */
const _money = new Intl.NumberFormat("en-US",
  { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const fmtMoney = (n) => (n === null || n === undefined) ? "—" : _money.format(n);
function fmtMoneyCompact(n) {
  if (n === null || n === undefined) return "—";
  if (Math.abs(n) >= 1_000_000) return `$${(n / 1_000_000).toFixed(1)}M`;
  if (Math.abs(n) >= 10_000) return `$${(n / 1_000).toFixed(1)}K`;
  return _money.format(n);
}
const fmtInt = (n) => (n === null || n === undefined) ? "—" : Number(n).toLocaleString("en-US");
const fmtPct = (x) => (x === null || x === undefined) ? "—" : `${Math.round(x * 100)}%`;
function fmtDur(secs) {
  if (secs === null || secs === undefined) return "—";
  const s = Math.round(secs);
  return s >= 60 ? `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s` : `${s}s`;
}
function fmtDateTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString("en-US",
    { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}
function fmtDay(isoDay) {
  const d = new Date(`${isoDay}T00:00:00`);
  return isNaN(d) ? isoDay : d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}
function timeAgo(iso) {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  if (isNaN(ms)) return iso;
  const mins = Math.floor(ms / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

/* ----------------------------------------------------- outcomes & states */
/* Fixed slot order — the color follows the outcome, never this week's rank. */
const OUTCOME_META = {
  booked:      { label: "Booked",      color: "var(--s1)" },
  transferred: { label: "Transferred", color: "var(--s2)" },
  no_deal:     { label: "No deal",     color: "var(--s3)" },
  rejected:    { label: "Rejected",    color: "var(--s4)" },
  abandoned:   { label: "Abandoned",   color: "var(--s5)" },
  incomplete:  { label: "Incomplete",  color: "var(--s-none)" },
};
const outcomeKey = (o) => o || "incomplete";
/* The exact set `CarrierSalesAgent._call_label` can return — see agent.py's
   `_END_REASON_LABELS`. Kept in this fixed order for the filter dropdown. */
const REASON_LABELS = ["Success", "Rate too high", "Carrier not qualified",
  "Ask for transfer to human", "Alternate dates", "User declined load", "Other"];
function outcomeChip(outcome) {
  const key = outcomeKey(outcome);
  const meta = OUTCOME_META[key] || { label: key };
  return el("span", { class: `chip ${key}` }, el("span", { class: "dot" }), meta.label);
}

/* A call with no outcome that started recently is LIVE — the worker persists
   the transcript turn by turn, so it can be watched as it happens. (Old
   outcome-less rows from before hangup-finalizing existed stay "Incomplete".) */
const LIVE_WINDOW_MS = 15 * 60 * 1000;
function isLiveRow(r) {
  return !r.outcome && r.start_time &&
    (Date.now() - new Date(r.start_time).getTime()) < LIVE_WINDOW_MS;
}
function statusChip(r) {
  if (isLiveRow(r)) {
    return el("span", { class: "chip live" }, el("span", { class: "dot" }), "Live");
  }
  return outcomeChip(r.outcome);
}

const STATE_LABELS = {
  greeting: "Greeting", identify_load: "Identifying load",
  verify_carrier: "Verifying carrier", ask_empty: "Truck location",
  check_requirements: "Requirements", state_price: "Opening rate",
  negotiate: "Negotiating", confirm_booking: "Confirming booking",
  confirm_email: "Confirming email", done: "Call complete",
};

/* ---------------------------------------------------------------- tooltip */
const tooltip = {
  node: null,
  show(evt, title, rows) {
    const t = this.node || (this.node = $("#tooltip"));
    t.replaceChildren(
      el("div", { class: "t-title" }, title),
      ...rows.map((r) => el("div", { class: "t-row" },
        r.color ? el("span", { class: "t-key", style: `background:${r.color}` }) : null,
        el("span", { class: "t-val" }, r.value),
        el("span", { class: "t-name" }, r.name))),
    );
    t.style.display = "block";
    const pad = 14;
    const rect = t.getBoundingClientRect();
    let x = evt.clientX + pad, y = evt.clientY + pad;
    if (x + rect.width > innerWidth - 8) x = evt.clientX - rect.width - pad;
    if (y + rect.height > innerHeight - 8) y = evt.clientY - rect.height - pad;
    t.style.left = `${x}px`;
    t.style.top = `${y}px`;
  },
  hide() { if (this.node) this.node.style.display = "none"; },
};

/* ------------------------------------------------------------------ theme */
function initTheme() {
  const saved = localStorage.getItem("lv-theme");
  if (saved) document.documentElement.dataset.theme = saved;
  $("#theme-toggle").addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("lv-theme", next);
  });
}

/* ----------------------------------------------------------------- charts */
function niceMax(n) {
  if (n <= 4) return 4;
  const pow = 10 ** Math.floor(Math.log10(n));
  for (const m of [1, 2, 2.5, 5, 10]) if (n <= m * pow) return m * pow;
  return 10 * pow;
}

/* Calls per day — single-series column chart (no legend: the title names it). */
function columnChart(data) {
  const W = 960, H = 230, L = 42, R = 8, T = 12, B = 26;
  const innerW = W - L - R, innerH = H - T - B;
  const yMax = niceMax(Math.max(...data.map((d) => d.calls), 1));
  const y = (v) => T + innerH - (v / yMax) * innerH;
  const slot = innerW / data.length;
  const barW = Math.min(24, slot * 0.62);
  const svg = svgEl("svg", { class: "chart-svg", viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": "Calls per day" });

  const ticks = 4;
  for (let i = 1; i <= ticks; i++) {
    const v = (yMax / ticks) * i, ty = y(v);
    svg.append(
      svgEl("line", { class: "gridline", x1: L, x2: W - R, y1: ty, y2: ty }),
      svgEl("text", { x: L - 7, y: ty + 3.5, "text-anchor": "end" }, fmtInt(v)));
  }
  svg.append(svgEl("line", { class: "baseline", x1: L, x2: W - R, y1: y(0), y2: y(0) }));

  const labelEvery = Math.max(1, Math.ceil(data.length / 7));
  data.forEach((d, i) => {
    const cx = L + slot * i + slot / 2;
    if (i % labelEvery === 0) {
      svg.append(svgEl("text", { x: cx, y: H - 8, "text-anchor": "middle" }, fmtDay(d.day)));
    }
    if (d.calls > 0) {
      const x0 = cx - barW / 2, yTop = y(d.calls), yBase = y(0);
      const r = Math.min(4, Math.max(yBase - yTop, 1));
      const bar = svgEl("path", { class: "bar", fill: "var(--s1)",
        d: `M${x0},${yBase} L${x0},${yTop + r} Q${x0},${yTop} ${x0 + r},${yTop} ` +
           `L${x0 + barW - r},${yTop} Q${x0 + barW},${yTop} ${x0 + barW},${yTop + r} ` +
           `L${x0 + barW},${yBase} Z` });
      svg.append(bar);
    }
    // Hit target is the whole slot, never just the painted bar.
    const hit = svgEl("rect", { class: "bar-hit", x: L + slot * i, y: T,
      width: slot, height: innerH + B, tabindex: "-1" });
    hit.addEventListener("mousemove", (evt) => tooltip.show(evt, fmtDay(d.day), [
      { color: "var(--s1)", value: fmtInt(d.calls), name: d.calls === 1 ? "call" : "calls" },
      { value: fmtInt(d.booked), name: "booked" },
    ]));
    hit.addEventListener("mouseleave", () => tooltip.hide());
    svg.append(hit);
  });
  return svg;
}

/* Outcome split — one horizontal stacked bar + legend that carries counts. */
function outcomeSplit(outcomes) {
  const total = outcomes.reduce((a, o) => a + o.count, 0);
  const wrap = el("div");
  if (!total) {
    wrap.append(el("div", { class: "empty" }, "No completed calls yet."));
    return wrap;
  }
  const bar = el("div", { class: "split-bar", role: "img", "aria-label": "Outcome split" });
  for (const o of outcomes) {
    if (!o.count) continue;
    const meta = OUTCOME_META[o.outcome];
    const seg = el("div", { class: "split-seg",
      style: `flex:${o.count} 1 0; background:${meta.color}` });
    seg.addEventListener("mousemove", (evt) => tooltip.show(evt, meta.label, [
      { color: meta.color, value: fmtInt(o.count), name: `calls · ${fmtPct(o.count / total)}` },
    ]));
    seg.addEventListener("mouseleave", () => tooltip.hide());
    bar.append(seg);
  }
  const legend = el("div", { class: "legend" },
    outcomes.map((o) => el("span", { class: "legend-item" },
      el("span", { class: "legend-swatch", style: `background:${OUTCOME_META[o.outcome].color}` }),
      `${OUTCOME_META[o.outcome].label} `,
      el("span", { class: "count" }, fmtInt(o.count)))));
  wrap.append(bar, legend);
  return wrap;
}

/* Negotiation ladder — agent vs carrier offers over the call. */
function ladderChart(offers) {
  const pts = offers.filter((o) => o.amount !== null && o.amount !== undefined);
  if (pts.length < 2) return null;
  const W = 560, H = 220, L = 52, R = 60, T = 14, B = 26;
  const innerW = W - L - R, innerH = H - T - B;
  const amounts = pts.map((p) => p.amount);
  const lo = Math.min(...amounts), hi = Math.max(...amounts);
  const padVal = Math.max((hi - lo) * 0.12, hi * 0.02, 1);
  const yLo = Math.max(0, lo - padVal), yHi = hi + padVal;
  const x = (i) => pts.length === 1 ? L + innerW / 2 : L + (i / (pts.length - 1)) * innerW;
  const y = (v) => T + innerH - ((v - yLo) / (yHi - yLo)) * innerH;

  const svg = svgEl("svg", { class: "chart-svg", viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": "Negotiation ladder" });
  for (let i = 0; i <= 3; i++) {
    const v = yLo + ((yHi - yLo) / 3) * i, ty = y(v);
    svg.append(
      svgEl("line", { class: "gridline", x1: L, x2: W - R, y1: ty, y2: ty }),
      svgEl("text", { x: L - 7, y: ty + 3.5, "text-anchor": "end" }, fmtMoney(Math.round(v))));
  }

  const SERIES = { agent: { color: "var(--s1)", label: "LaneVoice" },
                   carrier: { color: "var(--s2)", label: "Carrier" } };
  for (const [party, meta] of Object.entries(SERIES)) {
    const line = pts.map((p, i) => ({ p, i })).filter((d) => d.p.party === party);
    if (!line.length) continue;
    if (line.length > 1) {
      svg.append(svgEl("polyline", { points: line.map((d) => `${x(d.i)},${y(d.p.amount)}`).join(" "),
        fill: "none", stroke: meta.color, "stroke-width": 2,
        "stroke-linejoin": "round", "stroke-linecap": "round" }));
    }
    for (const d of line) {
      // 2px surface ring so dots stay legible where the two lines cross.
      svg.append(svgEl("circle", { cx: x(d.i), cy: y(d.p.amount), r: 4.5,
        fill: meta.color, stroke: "var(--surface)", "stroke-width": 2 }));
    }
    const last = line[line.length - 1];
    svg.append(svgEl("text", { x: x(last.i) + 9, y: y(last.p.amount) + 3.5,
      style: "font-weight:600; fill: var(--text-secondary)" },
      fmtMoney(last.p.amount)));
  }
  pts.forEach((p, i) => {
    const hit = svgEl("circle", { cx: x(i), cy: y(p.amount), r: 13, fill: "transparent" });
    const meta = SERIES[p.party] || { color: "var(--s-none)", label: p.party };
    hit.addEventListener("mousemove", (evt) => tooltip.show(evt, `Round ${p.round}`, [
      { color: meta.color, value: fmtMoney(p.amount), name: meta.label },
    ]));
    hit.addEventListener("mouseleave", () => tooltip.hide());
    svg.append(hit);
  });

  return el("div", {}, svg, el("div", { class: "legend" },
    Object.values(SERIES).map((s) => el("span", { class: "legend-item" },
      el("span", { class: "legend-swatch", style: `background:${s.color}; height:3px; border-radius:2px` }),
      s.label))));
}

/* Tiny trend — de-emphasis gray, current period in the series accent. */
function sparkline(values) {
  const W = 104, H = 30, P = 3;
  const hi = Math.max(...values, 1);
  const x = (i) => P + (i / Math.max(values.length - 1, 1)) * (W - 2 * P);
  const y = (v) => H - P - (v / hi) * (H - 2 * P);
  const svg = svgEl("svg", { class: "sparkline", width: W, height: H,
    viewBox: `0 0 ${W} ${H}`, "aria-hidden": "true" });
  svg.append(svgEl("polyline", {
    points: values.map((v, i) => `${x(i)},${y(v)}`).join(" "),
    fill: "none", stroke: "var(--muted)", "stroke-width": 1.6,
    "stroke-linejoin": "round", "stroke-linecap": "round" }));
  const li = values.length - 1;
  svg.append(svgEl("circle", { cx: x(li), cy: y(values[li]), r: 3,
    fill: "var(--s1)", stroke: "var(--surface)", "stroke-width": 2 }));
  return svg;
}

/* --------------------------------------------------------------- overview */
function kpiTile(label, value, sub, trend) {
  return el("div", { class: "card kpi" },
    el("div", { class: "kpi-head" },
      el("div", {},
        el("div", { class: "label" }, label),
        el("div", { class: "value" }, value)),
      trend || null),
    sub ? el("div", { class: "sub" }, sub) : null);
}

async function renderOverview(root) {
  const data = await api("/api/overview?days=30");
  const k = data.kpis;
  const spark = sparkline(data.calls_by_day.slice(-14).map((d) => d.calls));

  root.append(
    el("div", { class: "grid kpis" },
      kpiTile("Total calls", fmtInt(k.total_calls),
        `${fmtInt(k.completed)} completed`, spark),
      kpiTile("Booked", fmtInt(k.booked),
        k.booking_rate === null ? "no completed calls yet"
          : `${fmtPct(k.booking_rate)} of completed`),
      kpiTile("Transferred", fmtInt(k.transferred), "warm handoffs to a rep"),
      kpiTile("Avg call", fmtDur(k.avg_duration_secs),
        k.avg_rounds === null ? "—" : `${k.avg_rounds.toFixed(1)} negotiation rounds avg`),
      kpiTile("Booked value", fmtMoneyCompact(k.booked_value),
        k.avg_booked_rate === null ? "no bookings yet"
          : `${fmtMoney(Math.round(k.avg_booked_rate))} avg rate`)),
    el("div", { class: "grid", style: "grid-template-columns: minmax(0, 2fr) minmax(280px, 1fr); margin-top:14px" },
      el("div", { class: "card" },
        el("div", { class: "card-title" }, "Calls per day ",
          el("span", { class: "hint" }, "· last 30 days")),
        columnChart(data.calls_by_day)),
      el("div", { class: "card" },
        el("div", { class: "card-title" }, "Outcomes ",
          el("span", { class: "hint" }, "· all time")),
        outcomeSplit(data.outcomes))),
    el("div", { class: "card", style: "margin-top:14px" },
      el("div", { class: "card-title", style: "display:flex; justify-content:space-between" },
        el("span", {}, "Recent runs"),
        el("a", { href: "#/runs", style: "font-weight:550; color:var(--accent); text-decoration:none" },
          "View all →")),
      runsTable(data.recent, { compact: true })));
}

/* ------------------------------------------------------------------- runs */
function runsTable(rows, { compact = false } = {}) {
  if (!rows.length) {
    return el("div", { class: "empty" },
      el("div", { class: "big" }, "No calls yet"),
      "Take a test call in the playground — it runs the same agent the phone line does.");
  }
  const head = compact
    ? ["Started", "Caller", "Lane", "Carrier", "Outcome", "Reason", "Final rate"]
    : ["Started", "Run", "Caller", "Lane", "Carrier", "Outcome", "Reason", "Rounds", "Final rate", "Duration"];
  const table = el("table", {},
    el("thead", {}, el("tr", {}, head.map((h) =>
      el("th", { class: ["Rounds", "Final rate", "Duration"].includes(h) ? "num" : "" }, h)))),
    el("tbody", {}, rows.map((r) => {
      const caller = el("td", { class: "mono" },
        r.caller_number || el("span", { class: "dim" }, "—"));
      const lane = el("td", {},
        el("div", { class: "strong" }, r.lane || (r.load_id ? `Load ${r.load_id}` : "—")),
        r.lane && r.load_id ? el("div", { class: "dim mono" }, r.load_id) : null);
      const carrier = el("td", {},
        el("div", {}, r.carrier_name || (r.carrier_dot ? r.carrier_dot : el("span", { class: "dim" }, "—"))),
        r.carrier_mc ? el("div", { class: "dim mono" }, r.carrier_mc) : null);
      // Why the call ended, one tier finer than the Outcome chip next to it —
      // "Rate too high" / "Carrier not qualified" / "Other", with the one-line
      // reason underneath so "Other" is never a dead end. Null on a call still
      // in progress or finished before this existed.
      const reason = el("td", { style: "max-width:220px" },
        r.label ? el("div", { class: "strong" }, r.label) : el("span", { class: "dim" }, "—"),
        r.reason ? el("div", { class: "dim", style: "white-space:normal" }, r.reason) : null);
      const runId = el("td", { class: "mono" }, r.call_id,
        r.source === "playground"
          ? el("div", { class: "dim", style: "font-family:system-ui; font-size:11px" }, "▶ playground")
          : null);
      const cells = compact
        ? [el("td", { class: "dim", title: fmtDateTime(r.start_time) }, timeAgo(r.start_time)),
           caller, lane, carrier,
           el("td", {}, statusChip(r)),
           reason,
           el("td", { class: "num strong" }, r.final_rate ? fmtMoney(r.final_rate) : "—")]
        : [el("td", { class: "dim", title: r.start_time || "" }, fmtDateTime(r.start_time)),
           runId,
           caller, lane, carrier,
           el("td", {}, statusChip(r)),
           reason,
           el("td", { class: "num" }, r.rounds ?? "—"),
           el("td", { class: "num strong" }, r.final_rate ? fmtMoney(r.final_rate) : "—"),
           el("td", { class: "num" }, fmtDur(r.duration_secs))];
      return el("tr", { class: "rowlink", onclick: () => openCallDrawer(r.call_id) }, cells);
    })));
  return el("div", { class: "tablewrap" }, table);
}

async function renderRuns(root) {
  const state = { outcome: "", label: "", q: "", rendered: "" };
  const listWrap = el("div", { class: "card", style: "padding: 6px 4px" });

  async function refresh(force = false) {
    const params = new URLSearchParams({ limit: "200" });
    if (state.outcome) params.set("outcome", state.outcome);
    if (state.label) params.set("label", state.label);
    if (state.q) params.set("q", state.q);
    const rows = await api(`/api/calls?${params}`);
    // Re-render only on actual change — a poll that rebuilds identical rows
    // every few seconds would yank the table out from under a click.
    const fingerprint = JSON.stringify(rows);
    if (!force && fingerprint === state.rendered) return;
    state.rendered = fingerprint;
    listWrap.replaceChildren(runsTable(rows));
  }

  const select = el("select", { onchange: (e) => { state.outcome = e.target.value; refresh(); } },
    el("option", { value: "" }, "All outcomes"),
    Object.entries(OUTCOME_META).map(([key, meta]) =>
      el("option", { value: key }, meta.label)));
  const reasonSelect = el("select", { onchange: (e) => { state.label = e.target.value; refresh(); } },
    el("option", { value: "" }, "Any reason"),
    REASON_LABELS.map((label) => el("option", { value: label }, label)));
  let debounce;
  const search = el("input", { type: "search", placeholder: "Search runs, loads, carriers…",
    oninput: (e) => { state.q = e.target.value.trim(); clearTimeout(debounce); debounce = setTimeout(refresh, 250); } });

  root.append(
    el("div", { class: "filters" }, select, reasonSelect, search,
      el("button", { class: "btn", onclick: refresh }, "Refresh")),
    listWrap);
  await refresh();
  // Live calls persist their transcript turn by turn — poll so a run appears
  // the moment the phone is answered and its turn count grows during the call.
  _refreshTimer = setInterval(() => {
    if (!document.hidden) refresh().catch(() => {});
  }, 4000);
}

/* ---------------------------------------------------------------- drawer */
function metaItem(label, ...value) {
  return el("div", {}, el("div", { class: "m-label" }, label),
    el("div", { class: "m-value" }, ...value));
}

let _drawerTimer = null;

async function openCallDrawer(callId) {
  const drawer = $("#drawer"), veil = $("#drawer-veil");
  const stopPolling = () => { clearInterval(_drawerTimer); _drawerTimer = null; };
  const close = () => {
    stopPolling();
    drawer.classList.remove("open");
    veil.classList.remove("open");
  };
  stopPolling();
  veil.onclick = close;
  drawer.classList.add("open");
  veil.classList.add("open");
  drawer.replaceChildren(el("div", { class: "drawer-body dim" }, "Loading…"));

  let d;
  try { d = await api(`/api/calls/${encodeURIComponent(callId)}`); }
  catch (err) {
    drawer.replaceChildren(el("div", { class: "drawer-body" },
      el("div", { class: "error-note" }, String(err.message || err))));
    return;
  }

  let activeTab = "Transcript";
  const body = el("div", { class: "drawer-body" });

  function tabDefs() {
    return [
      ["Transcript", () => renderTranscriptTab(d)],
      ["Negotiation", () => renderNegotiationTab(d)],
      ["Timeline", () => renderTimelineTab(d)],
      ["Raw", () => el("pre", { class: "raw" }, JSON.stringify(d, null, 2))],
    ];
  }

  function renderBody() {
    const def = tabDefs().find(([name]) => name === activeTab) || tabDefs()[0];
    const follow = isLiveRow(d) && activeTab === "Transcript";
    body.replaceChildren(def[1]());
    if (follow) body.scrollTop = body.scrollHeight;   // tail the live call
  }

  function render() {
    const tabBar = el("div", { class: "tabs" }, tabDefs().map(([name]) =>
      el("button", { class: `tab${name === activeTab ? " active" : ""}`,
        onclick: () => { activeTab = name; render(); } }, name)));
    drawer.replaceChildren(
      el("div", { class: "drawer-head" },
        el("div", { class: "drawer-head-row" },
          el("div", { class: "drawer-title mono" }, d.call_id),
          statusChip(d),
          el("button", { class: "drawer-close", onclick: close, "aria-label": "Close" }, "×")),
        el("div", { class: "meta-grid" },
          metaItem("Started", fmtDateTime(d.start_time)),
          metaItem("Duration", fmtDur(d.duration_secs)),
          metaItem("Source", d.source === "playground" ? "Playground (test)" : "Phone line"),
          metaItem("Caller", d.caller_number || "—"),
          metaItem("Turns", d.turns ?? "—"),
          metaItem("Load", d.load_id || "—", d.lane ? el("div", { class: "dim" }, d.lane) : null),
          metaItem("Carrier", d.carrier_name || d.carrier_dot || "—",
            d.carrier_mc ? el("div", { class: "dim" }, `${d.carrier_mc} · ${d.carrier_dot}`) : null),
          metaItem("Final rate", d.final_rate ? fmtMoney(d.final_rate) : "—",
            d.rounds !== null && d.rounds !== undefined ? el("div", { class: "dim" }, `${d.rounds} rounds`) : null),
          metaItem("Reason", d.label || "—",
            d.reason ? el("div", { class: "dim" }, d.reason) : null)),
        d.has_recording ? recordingPlayer(
          `/api/calls/${encodeURIComponent(d.call_id)}/recording`) : null,
        tabBar),
      body);
    renderBody();
  }

  render();

  // A live call keeps its record growing turn by turn — follow it until the
  // outcome lands (or the drawer closes).
  if (isLiveRow(d)) {
    _drawerTimer = setInterval(async () => {
      if (document.hidden || !drawer.classList.contains("open")) return;
      let fresh;
      try { fresh = await api(`/api/calls/${encodeURIComponent(callId)}`); }
      catch { return; }   // one blip shouldn't kill the live view
      const grew = fresh.turns !== d.turns || fresh.outcome !== d.outcome ||
        fresh.offers.length !== d.offers.length || fresh.notes.length !== d.notes.length;
      d = fresh;
      if (grew) render();
      if (d.outcome) stopPolling();
    }, 3000);
  }
}

function bubble(who, text) {
  const isAgent = who === "agent";
  return el("div", { class: `bubble ${isAgent ? "agent" : "carrier"}` },
    el("div", { class: "avatar" }, isAgent ? "AI" : "C"),
    el("div", {},
      el("div", { class: "who" }, isAgent ? "LaneVoice" : "Caller"),
      el("div", { class: "msg" }, text)));
}

function renderTranscriptTab(d) {
  if (!d.transcript || !d.transcript.length) {
    return el("div", { class: "empty" },
      el("div", { class: "big" }, "No transcript stored"),
      "The call never reached end_call — it was dropped mid-flight or is still open.");
  }
  return el("div", { class: "bubbles" }, d.transcript.map(([who, line]) => bubble(who, line)));
}

function renderNegotiationTab(d) {
  const wrap = el("div");
  if (!d.offers.length) {
    return el("div", { class: "empty" },
      el("div", { class: "big" }, "No rates exchanged"),
      "The call ended before a dollar figure was put on the table.");
  }
  const chart = ladderChart(d.offers);
  if (chart) wrap.append(chart);
  wrap.append(el("div", { class: "tablewrap", style: "margin-top:12px" },
    el("table", {},
      el("thead", {}, el("tr", {},
        el("th", {}, "Round"), el("th", {}, "Party"),
        el("th", { class: "num" }, "Amount"), el("th", {}, "Time"))),
      el("tbody", {}, d.offers.map((o) => el("tr", {},
        el("td", { class: "num" }, o.round),
        el("td", {}, o.party === "agent" ? "LaneVoice" : "Carrier"),
        el("td", { class: "num strong" }, fmtMoney(o.amount)),
        el("td", { class: "dim" }, fmtDateTime(o.timestamp))))))));
  return wrap;
}

function renderTimelineTab(d) {
  const events = [
    ...d.notes.map((n) => ({ ts: n.timestamp, text: n.note, kind: "note" })),
    ...d.transfers.map((t) => ({ ts: t.timestamp, kind: "transfer",
      text: `Transferred to rep ${t.rep_id} — ${t.result}` })),
  ].sort((a, b) => String(a.ts).localeCompare(String(b.ts)));
  if (!events.length) {
    return el("div", { class: "empty" },
      el("div", { class: "big" }, "Nothing logged"),
      "Notes and transfer events land here as the agent works the call.");
  }
  return el("div", { class: "timeline" }, events.map((e) =>
    el("div", { class: `tl-item ${e.kind}` },
      el("div", { class: "tl-time" }, fmtDateTime(e.ts)),
      el("div", { class: "tl-text" }, e.text))));
}

/* ------------------------------------------------------------------ loads */
const LOAD_STATUS = {
  open:      { label: "Open",      color: "#0ca30c" },
  covered:   { label: "Covered",   color: "var(--s-none)" },
  not_ready: { label: "Not ready", color: "#fab219" },
  cancelled: { label: "Cancelled", color: "#d03b3b" },
};

async function renderLoads(root) {
  const data = await api("/api/loads");
  if (data.live_board) {
    root.append(el("div", { class: "banner" },
      el("span", {}, "ℹ️"),
      el("span", {}, "The live board is served by Transport Pro. This table shows the " +
        "local database — the offline seed loads the playground's test calls sell against.")));
  }
  if (!data.loads.length) {
    root.append(el("div", { class: "card" }, el("div", { class: "empty" },
      el("div", { class: "big" }, "No loads in the local database"),
      "Run `make initdb` (or start an offline playground call) to seed the demo board.")));
    return;
  }
  root.append(el("div", { class: "card", style: "padding: 6px 4px" },
    el("div", { class: "tablewrap" }, el("table", {},
      el("thead", {}, el("tr", {},
        ["Load", "Lane", "Pickup", "Equipment", "Commodity"].map((h) => el("th", {}, h)),
        ["Weight", "Miles", "Board rate", "Max buy"].map((h) => el("th", { class: "num" }, h)),
        el("th", {}, "Status"))),
      el("tbody", {}, data.loads.map((l) => {
        const status = LOAD_STATUS[l.status] || { label: l.status, color: "var(--s-none)" };
        return el("tr", {},
          el("td", { class: "mono strong" }, l.load_id),
          el("td", {}, el("div", { class: "strong" }, `${l.origin} → ${l.destination}`),
            l.notes ? el("div", { class: "dim", style: "max-width:340px" }, l.notes) : null),
          el("td", {}, l.pickup_date,
            l.pickup_window ? el("div", { class: "dim" }, l.pickup_window) : null),
          el("td", {}, l.equipment || "—"),
          el("td", {}, l.commodity || "—"),
          el("td", { class: "num" }, l.weight_lbs ? fmtInt(l.weight_lbs) : "—"),
          el("td", { class: "num" }, l.miles ? fmtInt(l.miles) : "—"),
          el("td", { class: "num" }, fmtMoney(l.open_rate)),
          el("td", { class: "num strong" }, fmtMoney(l.ceiling_rate)),
          el("td", {},
            el("span", { class: "chip" },
              el("span", { class: "dot", style: `background:${status.color}` }), status.label),
            l.is_posted ? null : el("div", { class: "dim", style: "margin-top:3px" }, "unposted")));
      }))))),
    el("div", { class: "dim", style: "margin-top:10px; font-size:12px" },
      "Board rate is the opening anchor; Max buy is the hard ceiling. Internal figures — the agent never reveals them."));
}

/* ------------------------------------------------------------- playground */
const pg = {
  session: null, msgs: [], facts: [], summary: null,
  busy: false, showFacts: true, error: null,
};

async function renderPlayground(root) {
  const config = await getConfig();
  const msgsBox = el("div", { class: "pg-msgs" });
  const stateChip = el("span", { class: "chip state" }, el("span", { class: "dot", style: "background: var(--accent)" }), "");
  const factsBox = el("div", {});
  const errBox = el("div", {});
  const input = el("input", { type: "text", placeholder: "Speak as the carrier… e.g. “calling about load L1001”",
    autocomplete: "off" });
  const sendBtn = el("button", { class: "btn primary" }, "Send");
  const hangBtn = el("button", { class: "btn danger" }, "Hang up");

  function setError(err) {
    errBox.replaceChildren(err ? el("div", { class: "error-note" }, String(err)) : "");
  }

  function syncChrome() {
    const s = pg.session;
    stateChip.replaceChildren(el("span", { class: "dot", style: "background: var(--accent)" }),
      s ? (STATE_LABELS[s.state] || s.state) : "No call");
    const active = !!s && s.state !== "done";
    input.disabled = sendBtn.disabled = !active || pg.busy;
    hangBtn.disabled = !active || pg.busy;
    factsBox.replaceChildren(...pg.facts.slice(-3).map((t) =>
      el("div", { class: "facts-turn" },
        el("div", { class: "facts-label" }, "Directive"),
        el("div", { class: "facts-text" }, t.directive),
        t.facts ? el("pre", { class: "facts" }, t.facts) : null,
        t.speakable ? el("div", { class: "facts-text", style: "margin-top:4px" },
          `May say: ${t.speakable}`) : null)));
  }

  function push(who, text) {
    pg.msgs.push([who, text]);
    msgsBox.append(bubble(who, text));
    msgsBox.scrollTop = msgsBox.scrollHeight;
  }

  function pushSummary(summary) {
    const card = el("div", { class: "summary-card" },
      el("div", { class: "h" }, "Call summary"),
      el("div", {}, outcomeChip(summary.outcome)),
      el("div", { class: "kv" }, el("span", { class: "k" }, "Call"), el("span", { class: "v mono" }, summary.call_id)),
      el("div", { class: "kv" }, el("span", { class: "k" }, "Load"), el("span", { class: "v" }, summary.load_id || "—")),
      el("div", { class: "kv" }, el("span", { class: "k" }, "Carrier"), el("span", { class: "v" }, summary.carrier || "—")),
      el("div", { class: "kv" }, el("span", { class: "k" }, "Turns"), el("span", { class: "v" }, summary.turns)),
      el("div", { class: "kv" }, el("span", { class: "k" }, "Booking link sent"),
        el("span", { class: "v" }, summary.booking_link_sent ? "yes" : "no")),
      el("button", { class: "btn", style: "margin-top:8px",
        onclick: () => openCallDrawer(summary.call_id) }, "Open run detail"));
    msgsBox.append(card);
    msgsBox.scrollTop = msgsBox.scrollHeight;
  }

  async function start(live) {
    setError(null);
    pg.busy = true; syncChrome();
    try {
      pg.session = await api("/api/playground/sessions", { method: "POST", body: { live } });
      pg.msgs = []; pg.facts = []; pg.summary = null;
      msgsBox.replaceChildren();
      renderChatShell();
      push("agent", pg.session.greeting);
      railInfo();
      input.focus();
    } catch (err) { setError(err.message || err); }
    finally { pg.busy = false; syncChrome(); }
  }

  async function send() {
    const text = input.value.trim();
    if (!text || !pg.session || pg.busy) return;
    input.value = "";
    push("carrier", text);
    pg.busy = true; syncChrome();
    const typing = el("div", { class: "bubble agent" },
      el("div", { class: "avatar" }, "AI"),
      el("div", {}, el("div", { class: "msg dim" }, "…")));
    msgsBox.append(typing);
    msgsBox.scrollTop = msgsBox.scrollHeight;
    try {
      const res = await api(`/api/playground/sessions/${pg.session.session_id}/turns`,
        { method: "POST", body: { text } });
      typing.remove();
      push("agent", res.reply);
      pg.session.state = res.state;
      pg.facts.push(...(res.facts || []));
      if (res.done) {
        pg.summary = res.summary;
        pushSummary(res.summary);
        pg.session.state = "done";
      }
    } catch (err) {
      typing.remove();
      setError(err.message || err);
    } finally { pg.busy = false; syncChrome(); input.focus(); }
  }

  async function hangup() {
    if (!pg.session) return;
    try { await api(`/api/playground/sessions/${pg.session.session_id}`, { method: "DELETE" }); }
    catch { /* already gone is fine */ }
    push("carrier", "(hung up)");
    pg.session.state = "done";
    syncChrome();
  }

  sendBtn.addEventListener("click", send);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
  hangBtn.addEventListener("click", hangup);

  /* --- layout --- */
  const chatCard = el("div", { class: "card pg-chat" });
  function renderChatShell() {
    chatCard.replaceChildren(
      el("div", { class: "pg-chat-head" },
        stateChip,
        pg.session ? el("span", { class: "dim mono" }, pg.session.call_id) : null,
        el("span", { style: "flex:1" }),
        el("button", { class: "btn", onclick: () => start(false) }, "New call"),
        hangBtn),
      msgsBox,
      el("div", { class: "pg-input" }, input, sendBtn),
      errBox);
  }
  function renderStartShell() {
    const buttons = [el("button", { class: "btn primary", onclick: () => start(false) },
      "Start test call · seed board")];
    if (config.transport_pro.enabled) {
      buttons.push(el("button", { class: "btn", style: "margin-left:9px",
        onclick: () => start(true) }, "Start live call · Transport Pro"));
    }
    chatCard.replaceChildren(el("div", { class: "pg-start" },
      el("div", { class: "h" }, "Take a test call"),
      el("div", { class: "p" },
        "You type as the carrier; the reply comes from the same CarrierSalesAgent the phone line runs — ",
        "same verification, same negotiation engine, same audit trail. Finished calls appear under Runs."),
      el("div", {}, ...buttons),
      config.transport_pro.enabled
        ? el("div", { class: "p", style: "margin-top:14px; font-size:12px" },
            "⚠ A live call sells against the real board and posts a real offer if you book it through.")
        : null,
      errBox));
  }

  const railSession = el("div", {});
  function railInfo() {
    const s = pg.session;
    railSession.replaceChildren(
      el("div", { class: "kv" }, el("span", { class: "k" }, "Data source"),
        el("span", { class: "v" }, s ? s.data_source : "—")),
      el("div", { class: "kv" }, el("span", { class: "k" }, "Composer"),
        el("span", { class: "v" }, s ? s.composer : "—")),
      el("div", { class: "kv" }, el("span", { class: "k" }, "Call ID"),
        el("span", { class: "v mono" }, s ? s.call_id : "—")));
  }
  railInfo();

  const resetNote = el("div", { class: "dim", style: "font-size:12px; margin-top:8px" });
  const rail = el("div", { class: "pg-rail" },
    el("div", { class: "card" }, el("div", { class: "card-title" }, "Session"), railSession),
    el("div", { class: "card" },
      el("div", { class: "card-title" }, "Turn data ",
        el("span", { class: "hint" }, "· what the composer was handed")),
      factsBox,
      el("div", { class: "dim", style: "font-size:12px; margin-top:8px" },
        "The FACTS block is the fetched data verbatim — the only load, carrier and " +
        "rate values the agent was allowed to speak this turn.")),
    el("div", { class: "card" },
      el("div", { class: "card-title" }, "Demo board"),
      el("button", { class: "btn", onclick: async (e) => {
        e.target.disabled = true;
        try { await api("/api/board/reset", { method: "POST" });
          resetNote.textContent = "Board re-seeded. Call history kept."; }
        catch (err) { resetNote.textContent = String(err.message || err); }
        finally { e.target.disabled = false; }
      } }, "Reset seed board"),
      resetNote,
      el("div", { class: "dim", style: "font-size:12px; margin-top:8px" },
        "Puts L1001–L1005 back on the board after test bookings cover them. Runs are never deleted.")));

  if (pg.session && pg.msgs.length) {
    renderChatShell();
    for (const [who, text] of pg.msgs) msgsBox.append(bubble(who, text));
    if (pg.summary) pushSummary(pg.summary);
    msgsBox.scrollTop = msgsBox.scrollHeight;
  } else {
    renderStartShell();
  }
  syncChrome();
  root.append(el("div", { class: "pg" }, chatCard, rail));
}

/* --------------------------------------------------------------- practice */
/* The roles flip here: the MODEL plays the customer (one of the mood profiles)
   and the HUMAN is the rep being evaluated. Server-side the profile's hidden
   facts and triggers never reach this page — the cards carry only what a rep
   could know before dialing a real prospect. */
const DIFFICULTY_META = {
  easy:   { label: "Easy",   color: "#0ca30c" },
  medium: { label: "Medium", color: "#fab219" },
  hard:   { label: "Hard",   color: "#d03b3b" },
};
function difficultyChip(diff) {
  const meta = DIFFICULTY_META[diff] || { label: diff, color: "var(--s-none)" };
  return el("span", { class: "chip" },
    el("span", { class: "dot", style: `background:${meta.color}` }), meta.label);
}

const END_REASON_LABELS = {
  ended: "You ended the call",
  hangup: "The customer hung up",
  turn_limit: "Turn limit reached",
  abandoned: "Abandoned",
};

/* The scorecard. Dimension keys are the stored contract (judge.py RUBRIC);
   labels are just presentation. */
const DIM_LABELS = {
  opening: "Opening & hook", discovery: "Discovery", listening: "Listening",
  objection_handling: "Objection handling", value: "Value proposition",
  composure: "Composure", closing: "Closing & next step", focus: "Persona focus",
};
const scoreColor = (s) => s >= 8 ? "#0ca30c" : s >= 5 ? "#fab219" : "#d03b3b";

function metricsRow(m) {
  const items = [];
  const add = (label, v) => {
    if (v !== null && v !== undefined) {
      items.push(el("span", { class: "chip" }, `${label}: ${v}`));
    }
  };
  add("talk ratio", m.talk_ratio === null || m.talk_ratio === undefined ? null : fmtPct(m.talk_ratio));
  add("questions", m.questions);
  if (m.wpm) add("pace", `${m.wpm} wpm`);
  add("fillers/min", m.fillers_per_min);
  // Acoustics — only present on voice sessions with judgeable clips.
  add("pauses", m.pause_ratio === null || m.pause_ratio === undefined ? null : fmtPct(m.pause_ratio));
  add("long pauses", m.long_pauses);
  add("hesitation", m.leading_hesitation_secs === null || m.leading_hesitation_secs === undefined
    ? null : `${m.leading_hesitation_secs}s`);
  add("duration", fmtDur(m.duration_secs));
  return el("div", { class: "report-metrics" }, items);
}

const DELIVERY_LABELS = { confidence: "Confidence", clarity: "Clarity",
  energy: "Energy", pace: "Pace", warmth: "Warmth" };

function deliverySection(d) {
  const sec = el("div", { class: "report-sec" },
    el("div", { class: "sec-h" }, "Voice delivery",
      d.overall === null || d.overall === undefined ? null
        : el("span", { class: "sec-score" }, ` · ${d.overall} / 10`)));
  if (d.delivery_error) {
    sec.append(el("div", { class: "dim", style: "font-size:12px" },
      "Vocal verdict unavailable for this session — the conversational scorecard above still stands."));
    return sec;
  }
  const scores = d.scores || {};
  sec.append(el("div", { class: "score-grid" },
    Object.entries(DELIVERY_LABELS).filter(([k]) => scores[k]).map(([k, label]) => {
      const s = scores[k];
      const val = s.score === null || s.score === undefined ? 0 : s.score;
      return el("div", { class: "score-row" },
        el("span", { class: "score-label" }, label),
        el("div", { class: "score-bar" },
          el("div", { class: "score-fill",
            style: `width:${val * 10}%; background:${scoreColor(val)}` })),
        el("span", { class: "score-num" }, s.score ?? "—"));
    })));
  const comments = Object.entries(DELIVERY_LABELS)
    .filter(([k]) => scores[k] && scores[k].comment)
    .map(([k, label]) => el("li", {},
      el("span", { class: "strong" }, `${label}: `), scores[k].comment));
  if (comments.length) sec.append(el("ul", {}, comments));
  if (d.coaching && d.coaching.length) {
    sec.append(el("div", { class: "sec-h", style: "margin-top:10px" }, "Vocal coaching"),
      el("ul", {}, d.coaching.map((c) => el("li", {}, c))));
  }
  return sec;
}

function reportCard(r) {
  const card = el("div", { class: "report-card" });
  if (r.judge_error) {
    card.append(
      el("div", { class: "h" }, "Scorecard unavailable"),
      el("div", { class: "dim", style: "font-size:12px; margin-top:4px" },
        "The judge failed on this session — the transcript is saved, so it can be re-scored."));
    if (r.metrics) card.append(metricsRow(r.metrics));
    return card;
  }
  card.append(el("div", { class: "report-head" },
    el("div", {},
      el("div", { class: "h" }, "Scorecard"),
      el("div", { class: "report-overall" },
        r.overall === null || r.overall === undefined ? "—" : String(r.overall),
        el("span", { class: "of" }, " / 10"))),
    el("span", { class: "chip" },
      el("span", { class: "dot", style: `background:${r.win_condition_met ? "#0ca30c" : "#d03b3b"}` }),
      r.win_condition_met ? "Goal achieved" : "Goal not reached")));
  const scores = r.scores || {};
  card.append(el("div", { class: "score-grid" },
    Object.entries(DIM_LABELS).filter(([k]) => scores[k]).map(([k, label]) => {
      const s = scores[k];
      const val = s.score === null || s.score === undefined ? 0 : s.score;
      return el("div", { class: "score-row", title: s.comment || "" },
        el("span", { class: "score-label" }, label),
        el("div", { class: "score-bar" },
          el("div", { class: "score-fill",
            style: `width:${val * 10}%; background:${scoreColor(val)}` })),
        el("span", { class: "score-num" }, s.score ?? "—"));
    })));
  if (r.metrics) card.append(metricsRow(r.metrics));
  if (r.summary) card.append(el("div", { class: "report-summary" }, r.summary));
  if (r.strengths && r.strengths.length) {
    card.append(el("div", { class: "report-sec" },
      el("div", { class: "sec-h" }, "Keep doing"),
      el("ul", {}, r.strengths.map((s) => el("li", {}, s)))));
  }
  if (r.improvements && r.improvements.length) {
    card.append(el("div", { class: "report-sec" },
      el("div", { class: "sec-h" }, "Work on"),
      r.improvements.map((imp) => el("div", { class: "improve" },
        el("div", { class: "strong" }, imp.what),
        imp.why ? el("div", { class: "dim", style: "font-size:12.5px" }, imp.why) : null,
        imp.quote ? el("div", { class: "improve-quote" }, `You said: “${imp.quote}”`) : null,
        imp.better_line ? el("div", { class: "improve-better" }, `Try: “${imp.better_line}”`) : null))));
  }
  if (r.delivery) card.append(deliverySection(r.delivery));
  return card;
}

/* How the manager email went, in one line. Accepts either the live
   `email_status` shape (end-of-call response) or the stored row fields. */
function emailStatusLine(status) {
  if (!status) return null;
  const to = status.emailed_to;
  const err = status.error || status.email_error;
  if (to) {
    return el("div", { class: "email-status ok" },
      `📧 Report emailed to ${status.manager_name ? status.manager_name + " " : ""}(${to})`);
  }
  if (err) return el("div", { class: "email-status err" }, `Report not emailed: ${err}`);
  return null;
}

/* A call-recording player (practice sessions and phone runs alike). The
   element removes itself if the recording is missing — a 404 must read as
   "nothing was recorded", not a broken player. */
function recordingPlayer(url) {
  const audio = el("audio", { controls: "", preload: "none",
    src: url, class: "call-recording" });
  // Chrome reports Infinity for OGG audio with no index (the phone-call
  // recordings), which renders the seekbar as a live stream. Seeking past the
  // end once forces the real duration to be computed.
  audio.addEventListener("loadedmetadata", () => {
    if (audio.duration === Infinity) {
      const restore = () => { audio.currentTime = 0; audio.removeEventListener("durationchange", restore); };
      audio.addEventListener("durationchange", restore);
      audio.currentTime = 1e10;
    }
  });
  const wrap = el("div", { class: "report-sec" },
    el("div", { class: "sec-h" }, "Call recording"), audio);
  audio.addEventListener("error", () => wrap.remove());
  return wrap;
}

const pr = { session: null, msgs: [], busy: false, summary: null, profiles: null,
  voice: (localStorage.getItem("lv-practice-voice") || "on") === "on",
  stream: null };

/* Mic lifecycle: requested before a voice session starts (so the permission
   prompt comes at a sensible moment), released the moment the session is over —
   nobody wants the tab's recording indicator lit while reading their summary. */
async function initMic() {
  if (pr.stream) return true;
  if (!navigator.mediaDevices || !window.MediaRecorder) return false;
  try {
    pr.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    return true;
  } catch { return false; }
}
function releaseMic() {
  closeCapture();
  if (pr.stream) { pr.stream.getTracks().forEach((t) => t.stop()); pr.stream = null; }
}

/* WAV capture at 16 kHz. WAV rather than MediaRecorder's webm, deliberately:
   it's the one format every audio-input model accepts (the vocal-delivery
   judge), and the server can read it for the acoustics without shipping
   ffmpeg. The worklet forwards raw Float32 blocks; assembly happens on stop,
   so seconds are exact (samples / rate), not a UI timer. */
let _audioCtx = null, _capturing = false, _captured = [];
const _WORKLET_SRC =
  'class C extends AudioWorkletProcessor{process(i){const c=i[0][0];' +
  'if(c)this.port.postMessage(c.slice(0));return true}}' +
  'registerProcessor("lv-capture",C);';

async function ensureCapture() {
  if (!pr.stream) return false;
  if (_audioCtx) {
    if (_audioCtx.state === "suspended") await _audioCtx.resume();
    return true;
  }
  try {
    _audioCtx = new AudioContext({ sampleRate: 16000 });
    await _audioCtx.audioWorklet.addModule(
      URL.createObjectURL(new Blob([_WORKLET_SRC], { type: "text/javascript" })));
    const node = new AudioWorkletNode(_audioCtx, "lv-capture");
    node.port.onmessage = (ev) => { if (_capturing) _captured.push(ev.data); };
    _audioCtx.createMediaStreamSource(pr.stream).connect(node);
    if (_audioCtx.state === "suspended") await _audioCtx.resume();
    return true;
  } catch { closeCapture(); return false; }
}
function closeCapture() {
  if (_audioCtx) _audioCtx.close().catch(() => {});
  _audioCtx = null; _capturing = false; _captured = [];
}
function wavFromCapture() {
  const total = _captured.reduce((n, c) => n + c.length, 0);
  const rate = _audioCtx ? _audioCtx.sampleRate : 16000;
  const pcm = new Int16Array(total);
  let off = 0;
  for (const chunk of _captured) {
    for (let i = 0; i < chunk.length; i++) {
      const s = Math.max(-1, Math.min(1, chunk[i]));
      pcm[off++] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
  }
  const header = new DataView(new ArrayBuffer(44));
  const tag = (o, s) => { for (let i = 0; i < s.length; i++) header.setUint8(o + i, s.charCodeAt(i)); };
  tag(0, "RIFF"); header.setUint32(4, 36 + pcm.length * 2, true); tag(8, "WAVE");
  tag(12, "fmt "); header.setUint32(16, 16, true); header.setUint16(20, 1, true);
  header.setUint16(22, 1, true); header.setUint32(24, rate, true);
  header.setUint32(28, rate * 2, true); header.setUint16(32, 2, true);
  header.setUint16(34, 16, true);
  tag(36, "data"); header.setUint32(40, pcm.length * 2, true);
  return { blob: new Blob([header.buffer, pcm.buffer], { type: "audio/wav" }),
           secs: total / rate };
}
function playAudio(res) {
  if (!res || !res.audio) return;
  new Audio(`data:${res.audio_mime || "audio/wav"};base64,${res.audio}`)
    .play().catch(() => { /* autoplay refusal: the text bubble still carries the turn */ });
}

async function renderPractice(root) {
  if (!pr.profiles) pr.profiles = (await api("/api/practice/profiles")).profiles;
  if (!pr.managers) pr.managers = await api("/api/practice/managers");

  const msgsBox = el("div", { class: "pg-msgs" });
  const errBox = el("div", {});
  const input = el("input", { type: "text", autocomplete: "off",
    placeholder: "You're the rep — make your pitch…" });
  const sendBtn = el("button", { class: "btn primary" }, "Send");
  const endBtn = el("button", { class: "btn danger" }, "End call");
  const talkBtn = el("button", { class: "btn talk" }, "🎙 Hold to talk");

  const setError = (err) =>
    errBox.replaceChildren(err ? el("div", { class: "error-note" }, String(err)) : "");

  function personaInitials(name) {
    return name.split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
  }

  function practiceBubble(who, text) {
    const p = pr.session.profile;
    const isCustomer = who === "customer";
    return el("div", { class: `bubble ${isCustomer ? "agent" : "carrier"}` },
      el("div", { class: "avatar" }, isCustomer ? personaInitials(p.persona_name) : "ME"),
      el("div", {},
        el("div", { class: "who" }, isCustomer ? p.persona_name : pr.session.rep_name),
        el("div", { class: "msg" }, text)));
  }

  function push(who, text) {
    pr.msgs.push([who, text]);
    msgsBox.append(practiceBubble(who, text));
    msgsBox.scrollTop = msgsBox.scrollHeight;
  }

  function syncChrome() {
    const active = !!pr.session && !pr.summary;
    input.disabled = sendBtn.disabled = !active || pr.busy;
    endBtn.disabled = !active || pr.busy;
    talkBtn.disabled = !active || pr.busy;
  }

  function pushSummary(s) {
    pr.summary = s;
    releaseMic();
    const card = el("div", { class: "summary-card" },
      el("div", { class: "h" }, "Practice session complete"),
      el("div", { class: "kv" }, el("span", { class: "k" }, "Profile"),
        el("span", { class: "v" }, s.profile_name)),
      el("div", { class: "kv" }, el("span", { class: "k" }, "Rep"),
        el("span", { class: "v" }, s.rep_name)),
      el("div", { class: "kv" }, el("span", { class: "k" }, "Your turns"),
        el("span", { class: "v" }, s.turns)),
      el("div", { class: "kv" }, el("span", { class: "k" }, "Duration"),
        el("span", { class: "v" }, fmtDur(s.duration_secs))),
      el("div", { class: "kv" }, el("span", { class: "k" }, "Ended"),
        el("span", { class: "v" }, END_REASON_LABELS[s.end_reason] || s.end_reason)),
      emailStatusLine(s.report && s.report.email_status));
    msgsBox.append(card);
    if (s.mode === "voice") {
      msgsBox.append(recordingPlayer(
        `/api/practice/sessions/${encodeURIComponent(s.session_id)}/recording`));
    }
    if (s.report) msgsBox.append(reportCard(s.report));
    msgsBox.scrollTop = msgsBox.scrollHeight;
  }

  async function start(profile) {
    const repName = ($("#pr-rep-name") ? $("#pr-rep-name").value : "").trim();
    if (!repName) { setError("Enter your name first — the report needs to know whose pitch this is."); return; }
    localStorage.setItem("lv-rep-name", repName);
    const managerId = $("#pr-manager") ? $("#pr-manager").value : "";
    localStorage.setItem("lv-practice-manager", managerId);
    setError(null);
    pr.busy = true;
    // Ask for the mic BEFORE the session exists: a denied permission should
    // downgrade this one session to typing, not strand a half-started call.
    let useVoice = pr.voice;
    if (useVoice && !(await initMic())) {
      useVoice = false;
      setError("Microphone unavailable — starting this session in text mode.");
    }
    try {
      pr.session = await api("/api/practice/sessions",
        { method: "POST",
          body: { profile_id: profile.id, rep_name: repName, voice: useVoice,
                  manager_id: managerId || null } });
      pr.msgs = []; pr.summary = null;
      msgsBox.replaceChildren();
      renderChatShell();
      push("customer", pr.session.opening);
      playAudio(pr.session);
      input.focus();
    } catch (err) { releaseMic(); setError(err.message || err); }
    finally { pr.busy = false; syncChrome(); }
  }

  /* --- push-to-talk --- */
  async function beginRec(e) {
    e.preventDefault();
    if (pr.busy || _capturing || !pr.session || pr.summary) return;
    if (!pr.stream) { setError("Microphone unavailable — type instead."); return; }
    if (!(await ensureCapture())) {
      setError("Audio capture unavailable in this browser — type instead.");
      return;
    }
    _captured = [];
    _capturing = true;
    talkBtn.classList.add("recording");
    talkBtn.textContent = "● Recording — release to send";
  }
  async function finishRec() {
    if (!_capturing) return;
    _capturing = false;
    talkBtn.classList.remove("recording");
    talkBtn.textContent = "🎙 Hold to talk";
    const { blob, secs } = wavFromCapture();
    _captured = [];
    if (secs < 0.4) return;                 // a slipped click, not a turn
    await sendVoice(blob, secs);
  }
  talkBtn.addEventListener("pointerdown", beginRec);
  talkBtn.addEventListener("pointerup", finishRec);
  talkBtn.addEventListener("pointerleave", finishRec);

  async function sendVoice(blob, secs) {
    if (!pr.session || pr.busy) return;
    setError(null);
    pr.busy = true; syncChrome();
    const typing = el("div", { class: "bubble carrier" },
      el("div", { class: "avatar" }, "ME"),
      el("div", {}, el("div", { class: "msg dim" }, "…transcribing")));
    msgsBox.append(typing);
    msgsBox.scrollTop = msgsBox.scrollHeight;
    try {
      const res = await fetch(`/api/practice/sessions/${pr.session.session_id}/turns`, {
        method: "POST",
        headers: { "Content-Type": blob.type.split(";")[0] || "audio/webm",
                   "X-Audio-Seconds": secs.toFixed(1) },
        body: blob,
      });
      let data = null;
      try { data = await res.json(); } catch { /* non-JSON error page */ }
      if (!res.ok) throw new Error((data && data.error) || `${res.status} ${res.statusText}`);
      typing.remove();
      push("rep", data.heard);
      push("customer", data.reply);
      playAudio(data);
      if (data.audio_error) setError("Voice reply unavailable this turn (text only).");
      if (data.done) pushSummary(data.summary);
    } catch (err) {
      typing.remove();
      setError(err.message || err);
    } finally { pr.busy = false; syncChrome(); }
  }

  async function send() {
    const text = input.value.trim();
    if (!text || !pr.session || pr.busy) return;
    input.value = "";
    push("rep", text);
    pr.busy = true; syncChrome();
    const typing = el("div", { class: "bubble agent" },
      el("div", { class: "avatar" }, personaInitials(pr.session.profile.persona_name)),
      el("div", {}, el("div", { class: "msg dim" }, "…")));
    msgsBox.append(typing);
    msgsBox.scrollTop = msgsBox.scrollHeight;
    try {
      const res = await api(`/api/practice/sessions/${pr.session.session_id}/turns`,
        { method: "POST", body: { text } });
      typing.remove();
      push("customer", res.reply);
      if (res.done) pushSummary(res.summary);
    } catch (err) {
      typing.remove();
      setError(err.message || err);
    } finally { pr.busy = false; syncChrome(); input.focus(); }
  }

  async function endCall() {
    if (!pr.session || pr.busy) return;
    pr.busy = true; syncChrome();
    // Scoring runs inside this request — a beat of honesty about the wait.
    const scoring = el("div", { class: "bubble agent" },
      el("div", { class: "avatar" }, "★"),
      el("div", {}, el("div", { class: "msg dim" }, "…scoring your call")));
    msgsBox.append(scoring);
    msgsBox.scrollTop = msgsBox.scrollHeight;
    try {
      const res = await api(`/api/practice/sessions/${pr.session.session_id}/end`,
        { method: "POST" });
      scoring.remove();
      pushSummary(res.summary);
    } catch (err) { scoring.remove(); setError(err.message || err); }
    finally { pr.busy = false; syncChrome(); }
  }

  sendBtn.addEventListener("click", send);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
  endBtn.addEventListener("click", endCall);

  /* --- layout --- */
  const shell = el("div", {});
  function renderChatShell() {
    const p = pr.session.profile;
    shell.replaceChildren(
      el("div", { class: "card pg-chat" },
        el("div", { class: "pg-chat-head" },
          el("span", { class: "chip state" },
            el("span", { class: "dot", style: "background: var(--accent)" }),
            `${p.persona_name} — ${p.title}, ${p.company}`),
          difficultyChip(p.difficulty),
          el("span", { style: "flex:1" }),
          el("button", { class: "btn", onclick: renderPickShell }, "New session"),
          endBtn),
        el("div", { class: "practice-goal" },
          el("span", { class: "k" }, "Your goal: "), p.win_condition),
        msgsBox,
        el("div", { class: "pg-input" },
          pr.session.mode === "voice" ? talkBtn : null, input, sendBtn),
        errBox));
    syncChrome();
  }
  function renderPickShell() {
    pr.session = null; pr.msgs = []; pr.summary = null;
    releaseMic();
    const savedName = localStorage.getItem("lv-rep-name") || "";
    const voiceToggle = el("input", { type: "checkbox", id: "pr-voice" });
    voiceToggle.checked = pr.voice;
    voiceToggle.addEventListener("change", () => {
      pr.voice = voiceToggle.checked;
      localStorage.setItem("lv-practice-voice", pr.voice ? "on" : "off");
    });
    const savedManager = localStorage.getItem("lv-practice-manager") || "";
    const managerPick = (pr.managers.managers && pr.managers.managers.length)
      ? el("select", { id: "pr-manager" },
          el("option", { value: "" }, "Don't email the report"),
          pr.managers.managers.map((m) => el("option",
            { value: m.id, selected: m.id === savedManager ? "" : null },
            `Email report to ${m.name}`)))
      : el("span", { class: "dim", style: "font-size:12px" },
          "No account managers configured — add them in practice/data/managers.toml to email reports.");
    shell.replaceChildren(
      el("div", { class: "banner" }, el("span", {}, "🎯"),
        el("span", {}, "Pick a customer mood and make your pitch. The customer is played " +
          "by the model, in character — it only opens up if you earn it. In voice mode " +
          "you hold the talk button and speak; the customer talks back. Every finished " +
          "session is scored — conversation and vocal delivery — with the recording " +
          "saved, and the report can be emailed straight to your account manager.")),
      el("div", { class: "practice-name card" },
        el("label", { for: "pr-rep-name", class: "k" }, "Your name"),
        el("input", { id: "pr-rep-name", type: "text", value: savedName,
          placeholder: "e.g. Jordan Reyes", autocomplete: "off" }),
        el("label", { for: "pr-voice", class: "k practice-voice-toggle" },
          voiceToggle, " 🎙 Voice mode — speak & listen"),
        managerPick,
        errBox),
      el("div", { class: "profile-grid" },
        pr.profiles.map((p) => el("div", { class: "card profile-card" },
          el("div", { class: "profile-head" },
            el("div", { class: "strong" }, p.name), difficultyChip(p.difficulty)),
          el("div", { class: "dim", style: "font-size:12px" },
            `${p.persona_name} — ${p.title}, ${p.company}`),
          el("div", { class: "dim", style: "font-size:12px" }, p.vertical),
          el("div", { class: "profile-blurb" }, p.blurb),
          el("div", { class: "profile-goal" },
            el("span", { class: "k" }, "Your goal: "), p.win_condition),
          el("button", { class: "btn primary", onclick: () => start(p) }, "Start call")))),
      reportsSection());
  }

  function reportsSection() {
    const wrap = el("div", { class: "card", style: "margin-top:16px" },
      el("div", { class: "card-title" }, "Recent sessions ",
        el("span", { class: "hint" }, "· click one for its scorecard")));
    api("/api/practice/reports?limit=15").then((data) => {
      if (!data.reports.length) {
        wrap.append(el("div", { class: "empty" }, "No practice sessions yet."));
        return;
      }
      wrap.append(el("div", { class: "tablewrap" }, el("table", {},
        el("thead", {}, el("tr", {},
          ["When", "Rep", "Profile", "Mode", "Ended"].map((h) => el("th", {}, h)),
          ["Turns", "Score"].map((h) => el("th", { class: "num" }, h)),
          el("th", {}, "Goal"))),
        el("tbody", {}, data.reports.map((row) => el("tr",
          { class: "rowlink", onclick: () => openReport(row.session_id) },
          el("td", { class: "dim", title: row.started_at }, timeAgo(row.started_at)),
          el("td", {}, row.rep_name),
          el("td", { class: "strong" }, row.profile_name),
          el("td", {}, row.mode === "voice" ? "🎙 voice" : "⌨ text"),
          el("td", { class: "dim" }, END_REASON_LABELS[row.end_reason] || row.end_reason),
          el("td", { class: "num" }, row.turns),
          el("td", { class: "num strong" },
            row.judge_error ? "⚠" : (row.overall ?? "—")),
          el("td", {}, row.win_condition_met === null ? "—"
            : row.win_condition_met ? "✅" : "✗")))))));
    }).catch(() => {
      wrap.append(el("div", { class: "empty" }, "Couldn't load the session list."));
    });
    return wrap;
  }

  async function openReport(sessionId) {
    let d;
    try { d = await api(`/api/practice/reports/${encodeURIComponent(sessionId)}`); }
    catch (err) { setError(err.message || err); return; }
    const bubbles = el("div", { class: "bubbles", style: "margin-top:8px" },
      (d.transcript || []).map(([who, line]) =>
        el("div", { class: `bubble ${who === "customer" ? "agent" : "carrier"}` },
          el("div", { class: "avatar" }, who === "customer" ? "C" : "ME"),
          el("div", {},
            el("div", { class: "who" }, who === "customer" ? "Customer" : d.rep_name),
            el("div", { class: "msg" }, line)))));
    shell.replaceChildren(el("div", { class: "card" },
      el("div", { class: "pg-chat-head" },
        el("span", { class: "chip state" },
          el("span", { class: "dot", style: "background: var(--accent)" }),
          `${d.profile_name} — ${d.rep_name}`),
        el("span", { class: "dim" }, fmtDateTime(d.started_at)),
        el("span", { style: "flex:1" }),
        el("button", { class: "btn", onclick: renderPickShell }, "← All sessions")),
      d.has_recording ? recordingPlayer(
        `/api/practice/sessions/${encodeURIComponent(d.session_id)}/recording`) : null,
      d.report ? emailStatusLine(d.report) : null,
      d.report ? reportCard(d.report)
               : el("div", { class: "empty" }, "No scorecard was produced for this session."),
      el("div", { class: "card-title", style: "margin-top:16px" }, "Transcript"),
      bubbles));
  }

  if (pr.session && pr.msgs.length) {
    renderChatShell();
    for (const [who, text] of pr.msgs) msgsBox.append(practiceBubble(who, text));
    if (pr.summary) { const s = pr.summary; pr.summary = null; pushSummary(s); }
    msgsBox.scrollTop = msgsBox.scrollHeight;
  } else {
    renderPickShell();
  }
  root.append(shell);
}

/* --------------------------------------------------------------- settings */
function kvRow(k, v) {
  return el("div", { class: "kv" }, el("span", { class: "k" }, k),
    el("span", { class: "v" }, v));
}
function onOff(on, onText, offText) {
  return el("span", {}, el("span", { class: `status-dot ${on ? "on" : "off"}` }),
    on ? onText : offText);
}

async function renderSettings(root) {
  const c = await getConfig(true);
  root.append(
    el("div", { class: "banner" }, el("span", {}, "🔒"),
      el("span", {}, "Read-only. Everything below comes from settings.py / .env — change a model or a knob there, restart, and this page follows. Secrets are never sent to the browser.")),
    el("div", { class: "grid config-grid" },
      el("div", { class: "card" },
        el("div", { class: "card-title" }, "Voice pipeline"),
        kvRow("STT", c.models.stt),
        kvRow("LLM", `${c.models.llm_provider} / ${c.models.llm}`),
        kvRow("LLM key", onOff(c.models.llm_key_present, "present", "missing — playground uses the offline stub")),
        kvRow("Phrasing via LLM", c.models.use_llm ? "on" : "off (fast templates)"),
        kvRow("TTS", c.models.tts),
        kvRow("Voice", c.models.tts_voice)),
      el("div", { class: "card" },
        el("div", { class: "card-title" }, "Data source"),
        kvRow("DATA_SOURCE", c.data_source),
        kvRow("Audit DB", c.db_path),
        kvRow("Transport Pro", onOff(c.transport_pro.enabled,
          c.transport_pro.url || "enabled", "off — offline seed board")),
        kvRow("Office scope", c.transport_pro.office_terminal_code || "whole company board"),
        kvRow("Sellable statuses", c.transport_pro.open_load_statuses.join(", ") || "—"),
        kvRow("Loads read aloud on a miss", c.transport_pro.max_offered_loads)),
      el("div", { class: "card" },
        el("div", { class: "card-title" }, "Negotiation engine"),
        kvRow("Max rounds", c.negotiation.max_rounds),
        kvRow("Reserve below Max Buy", fmtMoney(c.negotiation.buffer)),
        kvRow("Reciprocity (share returned)", c.negotiation.reciprocity),
        kvRow("Agent's own authority", `${c.negotiation.discretion_rate} of floor→Max Buy`),
        kvRow("Settle gap", c.negotiation.settle_gap_rate),
        kvRow("Split-close gap", c.negotiation.split_gap_rate),
        kvRow("Stonewall best-and-final", c.negotiation.stonewall_final_rate),
        kvRow("Holds before final", c.negotiation.max_holds)),
      el("div", { class: "card" },
        el("div", { class: "card-title" }, "Integrations"),
        kvRow("Highway vetting", onOff(c.integrations.highway,
          "on — qualifications + cargo insurance", "off — checks skipped, loudly")),
        kvRow("Booking links", onOff(c.integrations.happyrobot_booking_links,
          "on — real book-now links", "off — rates logged for a rep")),
        kvRow("LiveKit telephony", onOff(c.integrations.livekit, "configured", "not configured")))));
}

/* ------------------------------------------------------------ config cache */
let _config = null;
async function getConfig(force = false) {
  if (!_config || force) _config = await api("/api/config");
  return _config;
}

/* ----------------------------------------------------------------- router */
const ICONS = {
  overview: "M3 3h7v7H3zM14 3h7v4h-7zM14 10h7v11h-7zM3 13h7v8H3z",
  runs: "M4 5a2 2 0 0 1 2-2h2l2 5-2.5 1.5a12 12 0 0 0 5 5L14 12l5 2v2a2 2 0 0 1-2 2A15 15 0 0 1 4 5z",
  loads: "M3 7l9-4 9 4v10l-9 4-9-4zM3 7l9 4m0 0l9-4m-9 4v10",
  playground: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM10 8.5l6 3.5-6 3.5z",
  practice: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 7.5a4.5 4.5 0 1 0 0 9 4.5 4.5 0 0 0 0-9zM12 10.8a1.2 1.2 0 1 0 0 2.4 1.2 1.2 0 0 0 0-2.4z",
  settings: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM19 12a7 7 0 0 0-.1-1.2l2-1.5-2-3.5-2.4 1a7 7 0 0 0-2-1.2L14 3h-4l-.5 2.6a7 7 0 0 0-2 1.2l-2.4-1-2 3.5 2 1.5a7 7 0 0 0 0 2.4l-2 1.5 2 3.5 2.4-1a7 7 0 0 0 2 1.2L10 21h4l.5-2.6a7 7 0 0 0 2-1.2l2.4 1 2-3.5-2-1.5c.06-.4.1-.8.1-1.2z",
};
function icon(name) {
  return svgEl("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
    "stroke-width": "1.7", "stroke-linecap": "round", "stroke-linejoin": "round" },
    svgEl("path", { d: ICONS[name] }));
}

const PAGES = {
  overview: { title: "Overview", sub: "How the desk's AI agent is performing",
    render: renderOverview, nav: "Overview", section: "main" },
  runs: { title: "Runs", sub: "Every call the agent has taken, with its full audit trail",
    render: renderRuns, nav: "Runs", section: "main" },
  loads: { title: "Loads", sub: "The board the agent sells against",
    render: renderLoads, nav: "Loads", section: "main" },
  playground: { title: "Playground", sub: "Drive the real agent by text — same code path as the phone line",
    render: renderPlayground, nav: "Playground", section: "main" },
  practice: { title: "Practice", sub: "Pitch a simulated customer — every mood a freight desk actually meets",
    render: renderPractice, nav: "Practice", section: "main" },
  settings: { title: "Configuration", sub: "Models, knobs and integrations currently in force",
    render: renderSettings, nav: "Settings", section: "system" },
};

function buildNav() {
  const main = $("#nav-main"), system = $("#nav-system");
  for (const [key, page] of Object.entries(PAGES)) {
    const item = el("a", { class: "nav-item", href: `#/${key}`, dataset: { page: key } },
      icon(key), page.nav);
    (page.section === "system" ? system : main).append(item);
  }
}

let _refreshTimer = null;
async function navigate() {
  const key = (location.hash.replace(/^#\//, "") || "overview").split("?")[0];
  const page = PAGES[key] || PAGES.overview;
  document.querySelectorAll(".nav-item").forEach((n) =>
    n.classList.toggle("active", n.dataset.page === (PAGES[key] ? key : "overview")));
  $("#page-title").textContent = page.title;
  $("#page-sub").textContent = page.sub;

  const actions = $("#page-actions");
  actions.replaceChildren();
  if (page !== PAGES.playground) {
    actions.append(el("button", { class: "btn primary",
      onclick: () => { location.hash = "#/playground"; } }, "● New test call"));
  }

  clearInterval(_refreshTimer);
  _refreshTimer = null;
  const root = $("#page");
  root.replaceChildren();
  try {
    await page.render(root);
  } catch (err) {
    root.replaceChildren(el("div", { class: "card" },
      el("div", { class: "empty" },
        el("div", { class: "big" }, "Couldn't load this page"),
        String(err.message || err))));
    return;
  }
  if (page === PAGES.overview) {
    _refreshTimer = setInterval(async () => {
      if (document.hidden) return;
      const fresh = el("div");
      try { await renderOverview(fresh); root.replaceChildren(...fresh.children); }
      catch { /* keep the previous render on a blip */ }
    }, 30000);
  }
}

async function initSourcePill() {
  try {
    const c = await getConfig();
    $("#source-label").textContent = c.transport_pro.enabled
      ? "Transport Pro · live" : "Seed board · offline";
    $("#source-pill").title = `Audit DB: ${c.db_path}`;
  } catch {
    $("#source-label").textContent = "server unreachable";
    $("#source-pill").querySelector(".dot").style.background = "#d03b3b";
  }
}

initTheme();
buildNav();
initSourcePill();
addEventListener("hashchange", navigate);
navigate();
