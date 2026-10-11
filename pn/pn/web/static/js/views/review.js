// Hero review: each GTO deviation judged against the opponents actually faced.
import { h, api, link, navigate, fmtStat, num, pct, loading, errorBox } from "../core.js";
import { table, pageHead, empty, label, section } from "./ui.js";

export async function reviewPickView() {
  const players = await api("/api/players");
  const sel = h("select", { id: "hero", name: "hero", required: true },
    players.map((p) => h("option", { value: p.canonical_id }, `${p.name} (${p.hands} hands)`)));
  const form = h("form", {
    class: "inline-form",
    onsubmit: (e) => { e.preventDefault(); navigate(`/review/${encodeURIComponent(sel.value)}`); },
  }, h("label", { for: "hero" }, "Hero"), sel, h("button", { type: "submit", class: "btn" }, "Review"));
  return h("div", { class: "page" },
    pageHead("Hero review", "Whose deviations were justified?",
      "Every decision is graded best, mixed or blunder by the solver's frequency for the action taken, and each stat that strays from GTO is listed with its cost."),
    form);
}

function opponents(row) {
  if (!row.opponents || !row.opponents.length) return null;
  return h("ul", { class: "opps" }, row.opponents.map((o) => h("li", {},
    link(`/players/${encodeURIComponent(o.id)}`, o.name),
    ` ${num(o.share, 1)} opps · ${label(o.counter_stat || "")} ${o.counter_post === null || o.counter_post === undefined ? "–" : fmtStat(o.counter_stat || "", o.counter_post)}`,
    ` (n ${o.counter_n ?? "–"}, ${o.basis}, ${o.archetype})`)));
}

// ------------------------------------------------------------------ Blunders (pn ev run, frequency grades)
// Each graded decision is best / mixed / blunder by the solver's frequency for the chosen action
// (contracts/trainer.md "Plan 7 additions"); GTO is the only source.
const rate = (g) => (g.graded ? g.blunders / g.graded : null);

/** Groups with graded decisions only; `graded` cell reads "graded / decisions". */
function gradeTable(rows, key, title) {
  const shown = rows.filter((g) => g.graded);
  const hidden = rows.length - shown.length;
  const head = [[title, ""], ["graded", "graded / all decisions"], ["best", ""], ["mixed", ""], ["blund.", "blunders"], ["rate", "blunder rate"]]
    .map(([t, tip], i) => h("th", { scope: "col", class: i ? "num" : "", title: tip || null }, t));
  return h("div", {},
    shown.length ? table(head, shown.map((g) => h("tr", {}, h("th", { scope: "row" }, label(g[key])),
      h("td", { class: "num" }, `${g.graded} / ${g.n}`), h("td", { class: "num" }, g.best), h("td", { class: "num" }, g.mixed),
      h("td", { class: `num ${g.blunders ? "neg" : ""}` }, g.blunders), h("td", { class: "num" }, pct(rate(g))))), { class: "ev-table" })
      : empty(`No graded ${title} yet.`),
    hidden ? h("p", { class: "note" }, `${hidden} ${title === "street" ? "street" : "spot"}${hidden === 1 ? "" : "s"} without graded decisions not shown.`) : null);
}

function blunderBody(s, hero) {
  if (!s.decisions) return empty(`No graded decisions yet. Run: pn ev run ${hero}`);
  const top = s.top_hands.map((x) => h("li", {},
    link(`/hands/${encodeURIComponent(x.hand_id)}`, `#${x.hand_no}`),
    ` ${x.blunders} blunder${x.blunders === 1 ? "" : "s"} in ${x.decisions} graded decision${x.decisions === 1 ? "" : "s"}`));
  const notes = Object.entries(s.notes).map(([k, n]) => `${k.replaceAll("_", " ")} ${n}`).join(" · ");
  const off = s.unmodeled;
  return h("div", { class: "ev-body" },
    h("div", { class: "ev-totals" },
      h("p", { class: "ev-big" }, String(s.blunders), h("span", { class: "muted" }, ` blunder${s.blunders === 1 ? "" : "s"} in ${s.graded} graded decisions`)),
      h("p", {}, `blunder rate ${pct(s.blunder_rate, 1)} · ${num(s.blunders_per_100, 1)} blunders / 100 hands · ${s.hands} hands`),
      h("p", { class: "muted" }, `best ${s.best} · mixed ${s.mixed} · blunder ${s.blunders}`),
      s.null ? h("p", { class: "muted" }, `${s.null} decisions not graded: ${notes}`) : null,
      off.decisions ? h("p", { class: "muted", title: "hand outside the modeled range; the grade is unreliable" },
        `off-model: ${off.decisions} decisions, ${off.blunders} blunders, ${s.include_unmodeled ? "counted above" : "not counted above"}`) : null),
    h("div", { class: "ev-grid" }, gradeTable(s.by_street, "street", "street"), gradeTable(s.by_spot, "spot", "spot"),
      h("div", {}, h("h3", { class: "label ev-sub" }, "Hands with the most blunders"), top.length ? h("ol", { class: "ev-top" }, top) : empty("No blunders."))));
}

function blunderSection(hero) {
  const body = h("div", {}, loading());
  api(`/api/review/${encodeURIComponent(hero)}/ev?source=gto`)
    .then((s) => body.replaceChildren(blunderBody(s, hero)))
    .catch((e) => body.replaceChildren(errorBox(e)));
  return section("Blunders", h("p", { class: "note" }, "Solver grade of every decision: best, mixed or blunder by the frequency of the action taken."), body);
}

export async function reviewView({ hero }) {
  const r = await api(`/api/review/${encodeURIComponent(hero)}`);
  const counts = {};
  for (const row of r.rows) counts[row.verdict] = (counts[row.verdict] || 0) + 1;
  const rows = r.rows.flatMap((row, i) => {
    const detailId = `rv-${i}`;
    const more = h("tr", { class: "detail-row", id: detailId, hidden: true },
      h("td", { colspan: 9 }, h("p", {}, row.detail), row.exploit ? h("p", { class: "exploit" }, row.exploit) : null, opponents(row)));
    const toggle = h("button", { type: "button", class: "linkish", "aria-expanded": "false", "aria-controls": detailId,
      onclick: (e) => { const open = more.hidden; more.hidden = !open; e.currentTarget.setAttribute("aria-expanded", String(open)); },
    }, row.reason);
    return [h("tr", {},
      h("th", { scope: "row" }, label(row.stat)), h("td", {}, row.spot), h("td", {}, row.direction),
      h("td", { class: "num" }, fmtStat(row.stat, row.player_freq, 1)), h("td", { class: "num muted" }, fmtStat(row.stat, row.gto, 1)),
      h("td", { class: "num" }, row.n), h("td", { class: "num" }, num(row.cost, 2)),
      h("td", {}, h("span", { class: `verdict v-${row.verdict}` }, row.verdict)),
      h("td", { class: "reason", title: row.detail }, toggle)), more];
  });
  return h("div", { class: "page" },
    pageHead("Hero review", link(`/players/${encodeURIComponent(r.canonical_id)}`, r.display_name),
      `${r.hands} hands · `, Object.entries(counts).map(([v, n]) => h("span", { class: `verdict v-${v}` }, `${n} ${v}`))),
    blunderSection(r.canonical_id),
    r.rows.length
      ? table(["stat", "spot", "dir", "player", "GTO", "n", "cost", "verdict", "why (click for detail)"].map((t, i) =>
          h("th", { scope: "col", class: i >= 3 && i <= 6 ? "num" : "" }, t)), rows, { class: "review" })
      : empty("No leaks to review: no positional cell deviates from GTO with enough data."),
    h("p", { class: "note" }, "cost = CI-aware deviation from GTO × opportunities."));
}
