// Preflop: GTO Wizard mixed-frequency spots, pure chart files, and a free-text range grid.
import { h, api, pct, setQuery, showTip, hideTip, errorBox } from "../core.js";
import { renderGrid, rangeGrid, legend, actionColors } from "../grid.js";
import { pageHead, section } from "./ui.js";

const CODE_ORDER = ["A", "R", "C", "X", "F"]; // aggressive on the left, like GTO Wizard
const CODE_TOKEN = { A: "allin", R: "raise", C: "call", X: "check", F: "fold" };
const CODE_NAME = { A: "All-in", R: "Raise", C: "Call", X: "Check", F: "Fold" };

function codeLabel(spot, code) {
  if (spot.labels && spot.labels[code]) return spot.labels[code]; // PokerCoaching chart names
  const want = { A: "allin", R: "raise", C: "call", X: "check", F: "fold" }[code];
  return spot.actions.find((a) => a.toLowerCase().replace("-", "").startsWith(want)) || CODE_NAME[code];
}

/** 13x13 grid + legend for a GTO Wizard spot or a PokerCoaching chart spot (same shape:
 *  GET /api/gtow/spot, GET /api/charts/pc); the trainer feedback reuses it. */
export function spotGrid(spot, caption) {
  const codes = CODE_ORDER.filter((c) => Object.values(spot.cells).some((cell) => cell[c] > 0) || spot.summary[c]);
  const colors = actionColors(codes.map((c) => CODE_TOKEN[c]));
  const colorOf = Object.fromEntries(codes.map((c, k) => [c, colors[k]]));
  const cells = {};
  for (const [cls, sh] of Object.entries(spot.cells)) {
    cells[cls] = { weight: 1, segments: codes.map((c) => ({ freq: sh[c] || 0, color: colorOf[c] })),
      label: codes.filter((c) => sh[c]).map((c) => `${codeLabel(spot, c)} ${pct(sh[c])}`).join(", ") };
  }
  const tipFor = (cls) => {
    const sh = spot.cells[cls] || {};
    return h("div", {}, h("strong", {}, cls),
      h("ul", { class: "tip-list" }, codes.filter((c) => sh[c]).map((c) =>
        h("li", {}, h("span", { class: "swatch", style: { background: colorOf[c] } }), `${codeLabel(spot, c)} ${pct(sh[c], 1)}`))));
  };
  const grid = renderGrid(cells, { caption, onFocus: (cls, el) => showTip(el, tipFor(cls)), onBlur: hideTip });
  const leg = legend(codes.map((c) => codeLabel(spot, c)), codes.map((c) => colorOf[c]), codes.map((c) => pct(spot.summary[c] || 0, 1)));
  return { grid, leg };
}

function gtowPanel(spot) {
  const { grid, leg } = spotGrid(spot, `GTO Wizard ${spot.key}`);
  return h("div", { class: "chart-block" },
    h("p", { class: "chart-title" }, `${spot.depth}bb · ${spot.kind.replace("_", " ")} · ${spot.positions.join(" → ")} · actor ${spot.actor}`,
      spot.raise_to_bb ? ` · raise to ${spot.raise_to_bb}bb` : ""),
    h("div", { class: "grid-and-legend" }, grid, leg));
}

function rangePanel(res, title) {
  return h("div", { class: "chart-block" }, h("p", { class: "chart-title" }, title, ` · ${pct(res.width, 1)} of combos`),
    rangeGrid(res.grid, "#d4483f", { caption: title }),
    res.text ? h("p", { class: "range-text" }, res.text) : null);
}

async function into(holder, fn) {
  try { holder.replaceChildren(await fn()); } catch (e) { holder.replaceChildren(errorBox(e)); }
}

export async function chartsView(_params, q) {
  const [spots, files] = await Promise.all([api("/api/gtow/spots"), api("/api/charts")]);

  // ---- GTO Wizard spots
  const gtowOut = h("div", {});
  const depths = [...new Set(spots.map((s) => s.depth))].sort((a, b) => a - b);
  const spotSel = h("select", { id: "gtow-spot", onchange: () => showSpot(spotSel.value) },
    depths.map((d) => h("optgroup", { label: `${d} bb` }, spots.filter((s) => s.depth === d).map((s) =>
      h("option", { value: s.key }, `${s.kind_label}: ${s.positions.join(" vs ")}`)))));
  const showSpot = (key) => {
    setQuery({ spot: key });
    into(gtowOut, async () => gtowPanel(await api(`/api/gtow/spot?key=${encodeURIComponent(key)}`)));
  };
  const firstSpot = spots.some((s) => s.key === q.get("spot")) ? q.get("spot") : spots.length ? spots[0].key : null;
  if (firstSpot) { spotSel.value = firstSpot; showSpot(firstSpot); }
  else gtowOut.append(h("p", { class: "empty-note" }, "No GTO Wizard frequency file (data/gtowizard/*_freqs.json)."));

  // ---- pure chart files
  const fileNames = Object.keys(files.files);
  const chartOut = h("div", {});
  const keySel = h("select", { id: "chart-key", onchange: () => showChart() });
  const fileSel = h("select", { id: "chart-file", onchange: () => { fillKeys(); showChart(); } },
    fileNames.map((f) => h("option", { value: f }, f)));
  const fillKeys = () => keySel.replaceChildren(...(files.files[fileSel.value] || []).map((k) => h("option", { value: k }, k)));
  const showChart = () => {
    if (!fileSel.value || !keySel.value) return;
    setQuery({ file: fileSel.value, key: keySel.value });
    const qs = new URLSearchParams({ file: fileSel.value, key: keySel.value });
    into(chartOut, async () => rangePanel(await api(`/api/charts/grid?${qs}`), keySel.value));
  };
  if (fileNames.length) {
    fileSel.value = fileNames.includes(q.get("file")) ? q.get("file") : fileNames[0];
    fillKeys();
    if ((files.files[fileSel.value] || []).includes(q.get("key"))) keySel.value = q.get("key");
    showChart();
  } else chartOut.append(h("p", { class: "empty-note" }, "No chart files in config/preflop_charts."));

  // ---- free text
  const rangeOut = h("div", {});
  const input = h("input", { id: "range-text", type: "text", value: "22+,A2s+,KTs+,QTs+,JTs,ATo+,KJo+", spellcheck: "false", autocomplete: "off" });
  const showRange = () => into(rangeOut, async () => rangePanel(await api(`/api/range/grid?text=${encodeURIComponent(input.value)}`), "Custom range"));
  const form = h("form", { class: "inline-form", onsubmit: (e) => { e.preventDefault(); showRange(); } },
    h("label", { for: "range-text" }, "Range"), input, h("button", { type: "submit", class: "btn" }, "Show"));
  showRange();

  const field = (id, text, el) => h("label", { class: "field", for: id }, h("span", {}, text), el);
  return h("div", { class: "page" },
    pageHead("Preflop", "Charts", "GTO Wizard 6-max cash, 2.5x opens; pure charts are the max-frequency action per class."),
    section("GTO Wizard mixed strategy", h("div", { class: "filters" }, field("gtow-spot", "Spot", spotSel)), gtowOut),
    section("Pure chart", h("div", { class: "filters" }, field("chart-file", "File", fileSel), field("chart-key", "Chart", keySel)), chartOut),
    section("Range text", form, rangeOut));
}
