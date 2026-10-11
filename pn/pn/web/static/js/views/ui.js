// Small view helpers shared by the page modules.
import { h, fmtStat, pct } from "../core.js";

export const MIN_SIGNAL_N = 10; // same threshold as pn.profile.leaks.MIN_LEAK_N

/** Horizontally scrollable table wrapper; the page itself never scrolls sideways. */
export function table(head, rows, opts = {}) {
  return h("div", { class: "table-wrap" },
    h("table", { class: `data ${opts.class || ""}` },
      opts.caption ? h("caption", {}, opts.caption) : null,
      h("thead", {}, h("tr", {}, head.map((c) => (c instanceof Node ? c : h("th", { scope: "col" }, c))))),
      h("tbody", {}, rows)));
}

export function section(title, ...children) {
  return h("section", { class: "panel" }, h("h2", { class: "label panel-title" }, title), ...children);
}

export function pageHead(kicker, title, ...meta) {
  return h("header", { class: "page-head" },
    h("div", {}, h("span", { class: "label" }, kicker), h("h1", {}, title)),
    meta.length ? h("p", { class: "page-meta" }, ...meta) : null);
}

/** Direction vs GTO: "hi" (CI above), "lo" (CI below) or "" (CI contains GTO / thin sample). */
export function devClass(s) {
  if (!s || !s.dev_gto || s.n < MIN_SIGNAL_N) return "";
  return s.dev_gto > 0 ? "hi" : "lo";
}

/** A stat-object cell: posterior (n), GTO underneath, coloured by the CI test. */
export function statCell(stat, s) {
  if (!s || !s.n) return h("td", { class: "num empty-cell" }, "·");
  const title = `CI90 ${fmtStat(stat, s.ci90[0])}–${fmtStat(stat, s.ci90[1])}` +
    (s.gto !== null && s.gto !== undefined ? ` · GTO ${fmtStat(stat, s.gto)}` : "") +
    ` · prior ${fmtStat(stat, s.prior_mean)}`;
  return h("td", { class: `num stat ${devClass(s)}`, title },
    h("span", { class: "v" }, fmtStat(stat, s.post_mean)), h("span", { class: "n" }, ` ${s.n}`),
    s.gto !== null && s.gto !== undefined ? h("span", { class: "g" }, `g ${fmtStat(stat, s.gto)}`) : null);
}

export const label = (stat) => stat.replaceAll("_", " ");
export const pctOrDash = (x) => pct(x, 1);

export function empty(msg) {
  return h("p", { class: "empty-note" }, msg);
}
