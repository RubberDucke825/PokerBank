// Editable 13x13 range (weight 0-100 per hand class) kept in sync with a range-text box
// (contracts/range_format.md). Text edits are parsed by the server (/api/range/grid); grid
// edits rewrite the text from the class weights.
import { h, api, gridClass, num } from "./core.js";

const BRUSHES = [100, 75, 50, 25, 0];
const PARSE_DEBOUNCE_MS = 300;

export const classCombos = (cls) => (cls.length === 2 ? 6 : cls[2] === "s" ? 4 : 12);

/** {cls: 0..1} -> range text ("AA,KK:0.5,AKs"), strongest classes first in grid order. */
export function textFromWeights(weights) {
  const out = [];
  for (let i = 0; i < 13; i++) for (let j = 0; j < 13; j++) {
    const cls = gridClass(i, j);
    const w = weights[cls] || 0;
    if (w <= 0) continue;
    out.push(w >= 0.999 ? cls : `${cls}:${+w.toFixed(3)}`);
  }
  return out.join(",");
}

export const comboCount = (weights) =>
  Object.entries(weights).reduce((s, [cls, w]) => s + classCombos(cls) * w, 0);

/**
 * rangeEditor({id, label, text, onChange(text, weights)}) -> {el, setText(text), text()}.
 * Click or drag across cells to paint the brush weight; Enter/Space paints the focused cell.
 */
export function rangeEditor({ id, label, text = "", onChange }) {
  let weights = {};
  let current = text;
  let brush = 100;
  let painting = false;
  let seq = 0;
  let timer = null;
  const cells = new Map();
  const stat = h("span", { class: "re-stat mono" });
  const err = h("p", { class: "warn re-err", role: "alert" });
  const area = h("textarea", { id: `${id}-text`, class: "re-text", rows: 3, spellcheck: "false",
    "aria-label": `${label} range text`, placeholder: "e.g. 22+, A2s+, KTo+, QJs:0.5",
    oninput: () => { current = area.value.trim(); clearTimeout(timer); timer = setTimeout(parse, PARSE_DEBOUNCE_MS); } });

  const grid = h("div", { class: "grid13 re-grid", role: "grid", "aria-label": `${label} range, weight per hand class` });
  for (let i = 0; i < 13; i++) {
    const row = h("div", { class: "grid-row", role: "row" });
    for (let j = 0; j < 13; j++) {
      const cls = gridClass(i, j);
      const kind = i === j ? "pair" : i < j ? "suited" : "offsuit";
      const fill = h("span", { class: "re-fill", "aria-hidden": "true" });
      const sub = h("span", { class: "cell-sub" });
      const el = h("div", { class: `cell ${kind}`, role: "gridcell", tabindex: i === 0 && j === 0 ? 0 : -1,
        dataset: { cls, i, j } }, fill, h("span", { class: "cell-name" }, cls), sub);
      cells.set(cls, { el, fill, sub });
      row.append(el);
    }
    grid.append(row);
  }
  const paint = (cls) => {
    const w = brush / 100;
    if ((weights[cls] || 0) === w) return;
    weights = { ...weights, [cls]: w };
    if (!w) { const { [cls]: _, ...rest } = weights; weights = rest; }
    current = textFromWeights(weights);
    area.value = current;
    draw();
    emit();
  };
  grid.addEventListener("pointerdown", (e) => {
    const el = e.target.closest(".cell");
    if (!el) return;
    e.preventDefault();
    painting = true;
    paint(el.dataset.cls);
  });
  grid.addEventListener("pointerover", (e) => {
    const el = e.target.closest(".cell");
    if (painting && el) paint(el.dataset.cls);
  });
  window.addEventListener("pointerup", () => { painting = false; });
  grid.addEventListener("keydown", (e) => {
    const el = e.target.closest(".cell");
    if (!el) return;
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); paint(el.dataset.cls); return; }
    const moves = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] };
    if (!moves[e.key]) return;
    e.preventDefault();
    const i = Math.max(0, Math.min(12, +el.dataset.i + moves[e.key][0]));
    const j = Math.max(0, Math.min(12, +el.dataset.j + moves[e.key][1]));
    el.tabIndex = -1;
    const next = cells.get(gridClass(i, j)).el;
    next.tabIndex = 0;
    next.focus();
  });

  const brushSeg = h("div", { class: "seg re-brush", role: "group", "aria-label": "Brush weight" },
    BRUSHES.map((b) => h("button", { type: "button", class: "seg-opt", "aria-pressed": String(b === brush),
      onclick: (e) => {
        brush = b;
        brushSeg.querySelectorAll(".seg-opt").forEach((x) => x.setAttribute("aria-pressed", String(x === e.currentTarget)));
      } }, b ? `${b}%` : "Erase")));

  function draw() {
    for (const [cls, c] of cells) {
      const w = weights[cls] || 0;
      c.fill.style.transform = `scaleY(${w})`;
      c.el.classList.toggle("empty", !w);
      c.sub.textContent = w > 0 && w < 0.999 ? String(Math.round(100 * w)) : "";
      c.el.setAttribute("aria-label", `${cls} ${Math.round(100 * w)}%`);
    }
    const n = comboCount(weights);
    stat.textContent = `${num(n, 0)} combos · ${num((100 * n) / 1326, 1)}%`;
  }
  const emit = () => onChange && onChange(current, weights);

  async function parse() {
    const mine = ++seq;
    if (!current) { weights = {}; err.textContent = ""; area.removeAttribute("aria-invalid"); draw(); emit(); return; }
    try {
      const r = await api(`/api/range/grid?text=${encodeURIComponent(current)}`);
      if (mine !== seq) return;
      weights = r.grid || {};
      err.textContent = "";
      area.removeAttribute("aria-invalid");
      draw();
      emit();
    } catch (e) {
      if (mine !== seq) return;
      err.textContent = e.message;
      area.setAttribute("aria-invalid", "true");
    }
  }

  function setText(t) {
    current = (t || "").trim();
    area.value = current;
    return parse();
  }

  const el = h("div", { class: "range-editor" },
    h("div", { class: "re-head" }, brushSeg, stat), grid, area, err);
  draw();
  if (text) setText(text);
  return { el, setText, text: () => current, weights: () => weights };
}
