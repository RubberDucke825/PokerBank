// /train/performance (Plan 15): how close the trainer decisions are to GTO, per spot. KPI strip, a position x spot
// family heatmap (perf_heatmap.js), a closeness / blunder-rate trend and a detail panel for the picked cell with
// its last decisions and a "Drill this spot" button. Data: GET /api/train/performance (contracts/trainer.md
// "Plan 15 additions"). Filters live in the URL query (table, depth, window).
import { h, api, pct, setQuery, navigate, showTip, hideTip, errorBox, link, toast } from "../core.js";
import { pageHead, section, empty } from "./ui.js";
import { buildModel, heatmap, heatLegend, cellName, isThin, MIN_N } from "./perf_heatmap.js";
import { savedParams } from "./leaks_panel.js";
import { loadSetup, tokenName, SEATS } from "./train_util.js";

const FILTERS = [
  { key: "table", label: "Table", opts: [["", "All"], ["6", "6-max"], ["8", "8-max"]] },
  { key: "depth", label: "Depth", opts: [["", "All"], ["100", "100 bb"], ["200", "200 bb"]] },
  { key: "window", label: "Window", opts: [["250", "250"], ["1000", "1000"], ["", "All"]] },
];
const SVG_NS = "http://www.w3.org/2000/svg";
const GRADE_CLS = { best: "pos", mixed: "warn-ink", blunder: "neg" };

const fmtDate = (iso) => (iso ? String(iso).slice(0, 10) : "–");
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

function readFilters(q) {
  const out = {};
  for (const f of FILTERS) {
    const v = q.get(f.key) || "";
    out[f.key] = f.opts.some(([k]) => k === v) ? v : "";
  }
  return out;
}

// ------------------------------------------------------------------ KPIs
function kpis(t) {
  const kpi = (k, v, c, title) => h("div", { class: "kpi", title }, h("span", { class: "label" }, k), h("b", { class: c || "" }, v));
  const skipped = (t.legacy || 0) + (t.ungraded || 0);
  return h("div", { class: "kpis pf-kpis" },
    kpi("Decisions", t.decisions),
    kpi("Graded", t.graded, "", skipped ? `${t.legacy || 0} legacy (before frequency grading) and ${t.ungraded || 0} ungraded decisions are not scored` : null),
    kpi("GTO closeness", pct(t.closeness), "pf-kpi-main", "mean of freq(your action) / freq(solver's top action)"),
    kpi("Best · mixed · blunders", `${t.best} · ${t.mixed} · ${t.blunders}`),
    kpi("Blunder rate", t.graded ? pct(t.blunders / t.graded) : "–", t.blunders ? "neg" : ""),
    kpi("RNG match", pct(t.rng_match)));
}

// ------------------------------------------------------------------ trend
function svg(tag, attrs) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

/** Closeness and blunder rate per bucket of graded decisions, one 0-100% axis, oldest left; hover a bucket.
 *  The plot stretches to its box (preserveAspectRatio none, non-scaling strokes); axis labels are HTML. */
function trend(buckets) {
  const W = 1000, H = 100, PAD = 12; // user units; PAD keeps the end dots inside
  const x = (k) => (buckets.length === 1 ? W / 2 : PAD + (k * (W - 2 * PAD)) / (buckets.length - 1));
  const y = (v) => (1 - v) * H;
  const el = svg("svg", { class: "pf-trend-svg", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img",
    "aria-label": `GTO closeness and blunder rate by bucket of graded decisions, oldest first: ${buckets.map((b) =>
      `${b.bucket}: closeness ${pct(b.closeness)}, blunders ${pct(b.blunder_rate)}`).join("; ")}` });
  for (const v of [0, 0.5, 1]) el.append(svg("line", { class: "pf-grid", x1: 0, x2: W, y1: y(v), y2: y(v) }));
  const series = [["closeness", "pf-s-close"], ["blunder_rate", "pf-s-blunder"]];
  for (const [key, cls] of series) {
    const pts = buckets.map((b, k) => (b[key] === null || b[key] === undefined ? null : `${x(k).toFixed(1)},${y(b[key]).toFixed(1)}`)).filter(Boolean);
    if (pts.length > 1) el.append(svg("polyline", { class: `pf-line ${cls}`, points: pts.join(" ") }));
    if (pts.length) el.append(svg("path", { class: `pf-end ${cls}`, d: `M${pts[pts.length - 1]}l0,0` }));
  }
  const cross = svg("line", { class: "pf-cross", x1: 0, x2: 0, y1: 0, y2: H, visibility: "hidden" });
  el.append(cross);
  const step = buckets.length > 1 ? (W - 2 * PAD) / (buckets.length - 1) : W;
  buckets.forEach((b, k) => {
    const hit = svg("rect", { class: "pf-hit", x: x(k) - step / 2, y: 0, width: step, height: H });
    hit.addEventListener("pointerenter", () => {
      cross.setAttribute("x1", x(k)); cross.setAttribute("x2", x(k)); cross.setAttribute("visibility", "visible");
      showTip(hit, h("div", { class: "pf-tip" },
        h("div", { class: "pf-tip-name" }, `Decisions ${b.bucket}${b.date ? ` · to ${b.date}` : ""}`),
        h("ul", { class: "tip-list" },
          h("li", {}, h("span", { class: "pf-key-line pf-s-close" }), h("b", {}, pct(b.closeness)), " closeness"),
          h("li", {}, h("span", { class: "pf-key-line pf-s-blunder" }), h("b", {}, pct(b.blunder_rate)), " blunder rate"),
          h("li", { class: "muted" }, plural(b.n, "graded decision")))));
    });
    hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });
    el.append(hit);
  });
  const plot = h("div", { class: "pf-plot" },
    h("div", { class: "pf-yaxis", "aria-hidden": "true" }, ["100%", "50%", "0%"].map((t) => h("span", {}, t))), el);
  const last = buckets[buckets.length - 1];
  return h("figure", { class: "pf-trend" },
    h("ul", { class: "legend pf-trend-legend" },
      h("li", {}, h("span", { class: "pf-key-line pf-s-close", "aria-hidden": "true" }), "GTO closeness", h("b", {}, pct(last.closeness))),
      h("li", {}, h("span", { class: "pf-key-line pf-s-blunder", "aria-hidden": "true" }), "Blunder rate", h("b", {}, pct(last.blunder_rate)))),
    plot,
    h("figcaption", { class: "note" }, `${plural(buckets.length, "bucket")} of about ${buckets[0].n} graded decisions, oldest left · latest values in the key`));
}

// ------------------------------------------------------------------ detail
function gradeSplit(c) {
  const g = c.best + c.mixed + c.blunders;
  if (!g) return h("span", { class: "muted" }, "none graded");
  const seg = (n, cls) => (n ? h("span", { class: cls, style: { flexGrow: n } }) : null);
  return h("span", { class: "pf-split", role: "img", "aria-label": `best ${c.best}, mixed ${c.mixed}, blunders ${c.blunders}` },
    seg(c.best, "s-best"), seg(c.mixed, "s-mixed"), seg(c.blunders, "s-blunder"));
}

function closeBar(v) {
  return h("span", { class: "pf-close" },
    h("span", { class: "pf-close-bar", "aria-hidden": "true" }, h("i", { style: { width: `${Math.round(100 * (v || 0))}%` } })),
    h("b", {}, pct(v)));
}

const withFreq = (tok, f) => [tokenName(tok), h("small", { class: "muted" }, ` ${f === null || f === undefined ? "–" : pct(f)}`)];

/** The cell's newest graded decisions (GET /api/train/performance/recent, newest first, each with spot + label). */
function recentTable(recent) {
  if (!recent.length) return empty("No graded decisions here yet.");
  const multi = new Set(recent.map((d) => d.spot)).size > 1;
  return h("div", { class: "table-wrap pf-recent" }, h("table", { class: "data" },
    h("caption", {}, `Last ${plural(recent.length, "graded decision")}, newest first`),
    h("thead", {}, h("tr", {}, ["Date", "Hand", multi ? "Spot" : null, "You", "Solver best", "Grade"].filter(Boolean)
      .map((c) => h("th", { scope: "col" }, c)))),
    h("tbody", {}, recent.map((d) => h("tr", {},
      h("td", { class: "mono" }, fmtDate(d.played_at)), h("td", { class: "mono" }, d.hand_class),
      multi ? h("td", {}, d.label) : null,
      h("td", {}, withFreq(d.chosen, d.chosen_freq)), h("td", {}, withFreq(d.best, d.best_freq)),
      h("td", { class: GRADE_CLS[d.grade] || "" }, d.grade))))));
}

/** Drillable = a spot key in a known family ("other" rows have no dealable line). */
const drillable = (r) => !!r.spot && r.family !== "other";

/** Seat count for a drill: the table filter, else 8 when hero's seat or the preflop villain seat (pf|pos|faced|vs)
 *  only exists at 8-max, else the saved setup's table. */
function drillTable(filters, row) {
  if (filters.table) return +filters.table;
  const [kind, , , vs] = String(row.spot).split("|");
  const seats = [row.position, kind === "pf" && vs !== "-" ? vs : null].filter(Boolean);
  if (seats.some((p) => !SEATS[6].includes(p))) return 8;
  return loadSetup().table === 8 ? 8 : 6;
}

const drillDepth = (filters) => (filters.depth ? +filters.depth : loadSetup().depth === 200 ? 200 : 100);

/** Plan 16: the spots a drill can deal (GET /api/train/dealable per table x depth): {`${table}|${depth}|${spot}`: bool}.
 *  A failed check answers null, and the buttons stay (a click then explains why it cannot deal). */
async function dealable(rows, filters) {
  const groups = new Map();
  for (const r of rows) {
    const k = `${drillTable(filters, r)}|${drillDepth(filters)}`;
    groups.set(k, [...(groups.get(k) || []), r.spot]);
  }
  try {
    const out = {};
    await Promise.all([...groups].map(async ([k, spots]) => {
      const [table, depth] = k.split("|");
      const qs = new URLSearchParams([["table", table], ["depth", depth], ...[...new Set(spots)].map((s) => ["spot", s])]);
      const d = await api(`/api/train/dealable?${qs}`);
      for (const [s, ok] of Object.entries(d.dealable || {})) out[`${k}|${s}`] = ok;
    }));
    return out;
  } catch {
    return null;
  }
}

async function drill(row, filters, btn) {
  const setup = loadSetup();
  const table = drillTable(filters, row);
  const depth = drillDepth(filters);
  const opponents = setup.table === table && Array.isArray(setup.opponents) ? setup.opponents : Array(table - 1).fill(null);
  const body = { table, depth, source: "gto", hero_seat: null, opponents, rng_enabled: setup.rng_enabled !== false,
    drill_share: 0, drill_spot: row.spot, ...savedParams() };
  btn.disabled = true;
  try {
    const st = await api("/api/train/new", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
    navigate(`/train?sid=${encodeURIComponent(st.sid)}`);
  } catch (e) {
    btn.disabled = false;
    toast(`Cannot drill ${row.label}: ${e.message}`);
  }
}

function detail(pos, fam, cell, filters) {
  const rows = [...cell.rows].sort((a, b) => b.n - a.n);
  const btns = []; // [row, button]: shown once the dealable check answers (hidden when a pin would be refused)
  const recent = h("div", { class: "pf-recent-holder" }, h("p", { class: "loading" }, "Loading last decisions…"));
  const qs = new URLSearchParams({ ...Object.fromEntries(Object.entries(filters).filter(([, v]) => v)), position: pos, family: fam.id });
  api(`/api/train/performance/recent?${qs}`)
    .then((d) => recent.replaceChildren(recentTable(d.recent || [])))
    .catch((e) => recent.replaceChildren(errorBox(e)));
  const out = h("div", { class: "pf-detail" },
    h("div", { class: "pf-detail-head" },
      h("h3", { class: "pf-detail-title", id: "pf-detail-title" }, cellName(pos, fam)),
      h("span", { class: "muted mono" }, `${plural(cell.n, "decision")} · ${cell.graded} graded · closeness ${pct(cell.closeness)}`)),
    isThin(cell) ? h("p", { class: "note" }, `Thin sample: fewer than ${MIN_N} graded decisions, read the numbers loosely.`) : null,
    h("ul", { class: "pf-spots" }, rows.map((r) => {
      let btn = null;
      if (drillable(r)) {
        btn = h("button", { type: "button", class: "btn ghost pf-drill", "aria-label": `Drill ${r.label}`, hidden: true }, "Drill this spot");
        btn.addEventListener("click", () => drill(r, filters, btn));
        btns.push([r, btn]);
      }
      return h("li", { class: "pf-spot" },
        h("div", { class: "pf-spot-name" }, h("span", {}, r.label), h("small", { class: "muted mono" }, `${r.n} · last ${fmtDate(r.last_played)}`)),
        closeBar(r.closeness), gradeSplit(r),
        h("span", { class: "mono pf-spot-counts" }, `${r.best} · ${r.mixed} · `, h("span", { class: r.blunders ? "neg" : "" }, r.blunders)),
        btn);
    })),
    recent);
  if (btns.length) {
    dealable(btns.map(([r]) => r), filters).then((ok) => {
      for (const [r, btn] of btns) btn.hidden = ok !== null && !ok[`${drillTable(filters, r)}|${drillDepth(filters)}|${r.spot}`];
    });
  }
  return out;
}

// ------------------------------------------------------------------ page
function filterBar(get, onChange) {
  const filters = get();
  return h("div", { class: "filters pf-filters" }, FILTERS.map((f) => {
    const id = `pf-f-${f.key}`;
    const btns = f.opts.map(([k, t]) => h("button", { type: "button", class: "seg-opt", "aria-pressed": String(filters[f.key] === k),
      onclick: () => {
        const cur = get();
        if (cur[f.key] === k) return;
        btns.forEach((b, i) => b.setAttribute("aria-pressed", String(f.opts[i][0] === k)));
        onChange({ ...cur, [f.key]: k });
      } }, t));
    return h("div", { class: "pf-filter" }, h("span", { class: "label", id }, f.label), h("div", { class: "seg", role: "group", "aria-labelledby": id }, btns));
  }));
}

export async function performanceView(_params, q) {
  let filters = readFilters(q);
  let selected = q.get("cell") || null; // "pos:family" kept across filter changes when still present
  let seq = 0;
  const status = h("p", { class: "visually-hidden", role: "status" }); // announced instead of the whole body
  const body = h("div", { class: "pf-body" });
  const bar = filterBar(() => filters, (f) => { filters = f; setQuery({ ...filters }); load(); });
  const root = h("div", { class: "page pf-page" },
    pageHead("Trainer", "Performance",
      "GTO closeness per spot: how often your action is the solver's, weighted by its frequency. ",
      link("/train", "Back to the trainer")),
    bar, status, body);

  async function load() {
    const my = ++seq;
    body.replaceChildren(h("p", { class: "loading" }, "Loading performance…"));
    const qs = new URLSearchParams(Object.entries(filters).filter(([, v]) => v));
    let data;
    try { data = await api(`/api/train/performance?${qs}`); } catch (e) {
      if (my === seq) { body.replaceChildren(errorBox(e)); status.textContent = "Could not load performance"; }
      return;
    }
    if (my === seq) render(data);
  }

  function render(data) {
    hideTip();
    const t = data.totals || {};
    status.textContent = `Loaded ${plural(t.decisions || 0, "decision")}, ${t.graded || 0} graded`;
    if (!t.decisions) {
      body.replaceChildren(empty("No trainer decisions for these filters yet. Play some hands in the trainer to fill the chart."));
      return;
    }
    const model = buildModel(data);
    const panel = h("div", { class: "pf-detail-holder", role: "region", "aria-label": "Cell detail" });
    const pickCell = (pos, fam, cell, focus) => {
      selected = `${pos}:${fam.id}`;
      setQuery({ cell: selected });
      heatHolder.querySelectorAll("td.pf-cell").forEach((td) => {
        const on = td._pf.pos === pos && td._pf.fam.id === fam.id;
        td.classList.toggle("is-selected", on);
        td.setAttribute("aria-selected", String(on));
        td.tabIndex = on ? 0 : -1; // roving tabindex follows the picked cell
      });
      panel.replaceChildren(detail(pos, fam, cell, filters));
      if (focus) panel.scrollIntoView({ block: "nearest", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    };
    const cur = selected && selected.split(":");
    const curFam = cur && model.fams.find((f) => f.id === cur[1]);
    const curCell = curFam && model.cell(cur[0], curFam.id);
    if (!curCell) { selected = null; setQuery({ cell: null }); }
    const heatHolder = h("div", { class: "pf-heat-wrap", tabindex: -1 },
      heatmap(model, { selected, onPick: (pos, fam, cell) => pickCell(pos, fam, cell, true) }));
    if (curCell) panel.replaceChildren(detail(cur[0], curFam, curCell, filters));
    else panel.replaceChildren(empty("Pick a cell to see its spots, your last decisions and a drill button."));
    body.replaceChildren(
      kpis(t),
      section("By spot", heatLegend(), heatHolder,
        h("p", { class: "note" }, "Arrow keys move between cells, Enter opens one. Each cell: GTO closeness and number of graded decisions."),
        panel),
      section("Trend", data.trend && data.trend.length ? trend(data.trend) : empty("Not enough graded decisions for a trend yet.")));
  }

  await load();
  return root;
}
