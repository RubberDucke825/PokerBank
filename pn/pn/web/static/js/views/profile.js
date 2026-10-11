// Player profile: preflop vs GTO, PT4-style positional matrix, vs-opener splits, leaks, tips.
import { h, api, link, fmtStat, pct } from "../core.js";
import { table, section, pageHead, statCell, label, empty } from "./ui.js";

const COLUMN_ORDER = ["UTG", "UTG1", "UTG2", "UTG3", "LJ", "HJ", "CO", "BTN", "SB", "BB", "STR",
  "BTN_HU", "BB_HU", "IP", "OOP"];
const GROUPS = ["EP", "MP", "CO", "BTN", "SB", "BTN_HU"];
const ROW_GROUPS = [
  ["Preflop", ["vpip", "pfr", "rfi", "limp", "steal", "cold_call", "three_bet", "fold_to_three_bet", "four_bet",
    "fold_to_four_bet", "squeeze", "fold_to_steal", "fold_to_squeeze", "call_vs_squeeze"]],
  ["Flop", ["cbet_flop", "fold_to_cbet_flop", "raise_cbet_flop", "bet_vs_missed_cbet_flop", "check_raise_flop",
    "donk_flop", "agg_freq_flop", "af_flop"]],
  ["Turn", ["cbet_turn", "fold_to_cbet_turn", "raise_cbet_turn", "bet_vs_missed_cbet_turn", "float_turn",
    "probe_turn", "agg_freq_turn", "af_turn"]],
  ["River", ["cbet_river", "fold_to_cbet_river", "raise_cbet_river", "agg_freq_river", "af_river"]],
  ["Showdown", ["wtsd", "wsd", "wwsf"]],
];

const orderCols = (keys) => [...COLUMN_ORDER.filter((c) => keys.includes(c)), ...keys.filter((c) => !COLUMN_ORDER.includes(c)).sort()];
const colHead = (c) => h("th", { scope: "col", class: `num ${c === "IP" || c === "OOP" ? "col-split" : ""}` }, c.replace("_HU", " HU"));

function positionMatrix(bp) {
  const cols = orderCols(Object.keys(bp));
  const known = new Set(ROW_GROUPS.flatMap(([, r]) => r));
  const extra = [...new Set(cols.flatMap((c) => Object.keys(bp[c])))].filter((s) => !known.has(s)).sort();
  const groups = extra.length ? [...ROW_GROUPS, ["Other", extra]] : ROW_GROUPS;
  const rows = [];
  for (const [name, stats] of groups) {
    const live = stats.filter((s) => cols.some((c) => bp[c][s] && bp[c][s].n));
    if (!live.length) continue;
    rows.push(h("tr", { class: "group-row" }, h("th", { scope: "rowgroup", colspan: cols.length + 1 }, name)));
    for (const s of live) rows.push(h("tr", {}, h("th", { scope: "row" }, label(s)), cols.map((c) => statCell(s, bp[c][s]))));
  }
  return table([h("th", { scope: "col" }, "stat"), ...cols.map(colHead)], rows, { class: "matrix" });
}

function vsOpenerTable(vs) {
  const cols = orderCols(Object.keys(vs));
  if (!cols.length) return empty("No opportunities facing a single open yet.");
  const rows = [];
  for (const stat of ["three_bet", "cold_call", "fold_to_steal"]) {
    for (const g of GROUPS) {
      if (!cols.some((c) => vs[c][g] && vs[c][g][stat])) continue;
      rows.push(h("tr", {}, h("th", { scope: "row" }, `${label(stat)} vs ${g.replace("_HU", " HU")}`),
        cols.map((c) => statCell(stat, vs[c][g] && vs[c][g][stat]))));
    }
  }
  return table([h("th", { scope: "col" }, "facing open from"), ...cols.map(colHead)], rows, { class: "matrix" });
}

function preflopVsGto(p) {
  const dev = (d) => (d === null || d === undefined ? "–" : d === 0 ? "0" : `${d > 0 ? "+" : ""}${(100 * d).toFixed(0)}`);
  const devTd = (d) => h("td", { class: `num ${d > 0 ? "hi" : d < 0 ? "lo" : ""}` }, dev(d));
  const rows = Object.entries(p.preflop_vs_gto).map(([stat, e]) => h("tr", {},
    h("th", { scope: "row" }, label(stat)),
    h("td", { class: "num" }, fmtStat(stat, e.player, 1)), h("td", { class: "num muted" }, e.n),
    h("td", { class: "num" }, fmtStat(stat, e.gto_weighted, 1)), devTd(e.dev_gto_weighted),
    h("td", { class: "num muted" }, fmtStat(stat, e.gto_table_wide, 1)), devTd(e.dev_gto_table_wide)));
  return table(["stat", "player", "n", "GTO (your seats)", "dev", "GTO 6-max table-wide", "dev"].map((t, i) =>
    h("th", { scope: "col", class: i ? "num" : "" }, t)), rows);
}

function leakList(leaks) {
  if (!leaks.length) return empty("No positional cell has n ≥ 10 with a 90% CI that excludes GTO.");
  return h("ol", { class: "leaks" }, leaks.map((lk) => h("li", { class: `leak ${lk.direction === "over" ? "hi" : "lo"}` },
    h("div", { class: "leak-head" },
      h("strong", {}, label(lk.stat)), h("span", { class: "spot" }, lk.spot),
      h("span", { class: "leak-num" }, `${fmtStat(lk.stat, lk.player_freq)} vs GTO ${fmtStat(lk.stat, lk.gto)}`,
        h("span", { class: "muted" }, ` · n ${lk.n} · pop ${lk.population === null || lk.population === undefined ? "–" : fmtStat(lk.stat, lk.population)}`))),
    lk.exploit ? h("p", { class: "exploit" }, lk.exploit) : null)));
}

export async function profileView({ pid }) {
  const p = await api(`/api/players/${encodeURIComponent(pid)}/profile`);
  const arch = p.archetype;
  return h("div", { class: "page" },
    pageHead("Player profile", p.display_name,
      h("span", { class: `arch arch-${arch.label}` }, arch.label.replaceAll("_", " ")),
      ` confidence ${arch.confidence.toFixed(2)} · ${p.hands} hands · ${p.showdowns_seen} showdowns · priors ${p.priors_source} · GTO ${p.gto_source} · `,
      link(`/review/${encodeURIComponent(p.canonical_id)}`, "hero review →"), " ",
      link(`/hands?player=${encodeURIComponent(p.canonical_id)}`, "hands →")),
    p.tips && p.tips.length ? section("Exploits", h("ul", { class: "tips" }, p.tips.map((t) => h("li", {}, t)))) : null,
    section("Preflop vs GTO", h("p", { class: "note" }, "GTO weighted by the situations this player actually faced."), preflopVsGto(p)),
    section("By position", h("p", { class: "note" }, "Posterior % with opportunities; ", h("span", { class: "hi" }, "red"),
      " = 90% CI above GTO, ", h("span", { class: "lo" }, "blue"), " = below (n ≥ 10). IP/OOP columns are postflop."), positionMatrix(p.by_position)),
    section("Facing an open, by opener", vsOpenerTable(p.by_position_vs_opener)),
    section("Leaks", h("p", { class: "note" }, "Ranked by deviation from GTO; positional cells only."), leakList(p.leaks)),
    section("Archetype scores", h("dl", { class: "scores" }, Object.entries(arch.scores).map(([k, v]) => [
      h("dt", {}, k.replaceAll("_", " ")), h("dd", {}, h("span", { class: "bar", style: { width: pct(v) } }), pct(v))]))));
}
