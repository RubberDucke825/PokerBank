// Hand list with filters (state in the query string) and a street-by-street replay.
import { h, api, link, cards, money, signClass, setQuery } from "../core.js";
import { table, pageHead, section, empty } from "./ui.js";

const PAGE = 50;
const chips = (c) => ((c || 0) / 100).toFixed(2);
const bbs = (cents, bb) => (bb ? `${(cents / bb).toFixed(1)} bb` : money(cents));

function boardCards(b) {
  return cards([...(b.flop || []), ...(b.turn ? [b.turn] : []), ...(b.river ? [b.river] : [])]);
}

function handRows(items) {
  return items.map((x) => h("tr", {},
    h("th", { scope: "row" }, link(`/hands/${encodeURIComponent(x.hand_id)}`, `#${x.hand_no}`)),
    h("td", { class: "muted" }, x.started_at ? x.started_at.slice(0, 16).replace("T", " ") : "–"),
    h("td", { class: "num" }, x.n_players),
    h("td", {}, x.hero ? [h("span", { class: "pos-tag" }, x.hero.position), " ", cards(x.hero.cards)] : "–"),
    h("td", {}, boardCards(x.board)),
    h("td", {}, x.street, x.showdown ? h("span", { class: "badge" }, "SD") : null),
    h("td", { class: "num" }, bbs(x.pot, x.bb)),
    h("td", { class: `num ${x.hero ? signClass(x.hero.net) : ""}` }, x.hero ? bbs(x.hero.net, x.bb) : "–"),
    h("td", {}, x.winners.join(", "))));
}

export async function handsView(_params, q) {
  const players = await api("/api/players");
  const state = { player: q.get("player") || "", street: q.get("street") || "", showdown: q.get("showdown") || "",
    offset: Math.max(0, parseInt(q.get("offset") || "0", 10) || 0) };
  const out = h("div", { class: "hands-out" });

  async function load() {
    setQuery({ ...state, offset: state.offset || null });
    const qs = new URLSearchParams({ limit: PAGE, offset: state.offset });
    for (const k of ["player", "street", "showdown"]) if (state[k]) qs.set(k, state[k]);
    out.replaceChildren(h("p", { class: "loading" }, "Loading…"));
    let res;
    try { res = await api(`/api/hands?${qs}`); } catch (e) {
      out.replaceChildren(h("div", { class: "error", role: "alert" }, e.message)); return;
    }
    const last = Math.min(res.offset + res.items.length, res.total);
    const pager = h("div", { class: "pager" },
      h("button", { type: "button", class: "btn ghost", disabled: res.offset === 0,
        onclick: () => { state.offset = Math.max(0, state.offset - PAGE); load(); } }, "← newer"),
      h("span", {}, res.total ? `${res.offset + 1}–${last} of ${res.total}` : "0 hands"),
      h("button", { type: "button", class: "btn ghost", disabled: last >= res.total,
        onclick: () => { state.offset += PAGE; load(); } }, "older →"));
    out.replaceChildren(pager, res.items.length
      ? table(["hand", "time", "plrs", "player", "board", "street", "pot", "net", "won by"].map((t, i) =>
        h("th", { scope: "col", class: [2, 6, 7].includes(i) ? "num" : null }, t)), handRows(res.items))
      : empty("No hands match these filters."));
  }

  const select = (name, labelText, options) => {
    const el = h("select", { id: `f-${name}`, onchange: () => { state[name] = el.value; state.offset = 0; load(); } },
      options.map(([v, t]) => h("option", { value: v, selected: state[name] === v }, t)));
    return h("label", { class: "field" }, h("span", {}, labelText), el);
  };
  const filters = h("div", { class: "filters", role: "group", "aria-label": "Filters" },
    select("player", "Player", [["", "anyone"], ...players.map((p) => [p.canonical_id, p.name])]),
    select("street", "Reached", [["", "any street"], ["flop", "flop"], ["turn", "turn"], ["river", "river"]]),
    select("showdown", "Showdown", [["", "either"], ["true", "yes"], ["false", "no"]]));
  load();
  return h("div", { class: "page" }, pageHead("Hand history", "Hands"), filters, out);
}

// Solver grade per decision (pn ev run, contracts/trainer.md "Plan 7 additions"): best (green), mixed (amber),
// blunder (red), with the solver's frequency for the chosen action and for the most frequent action.
const GRADE_CLS = { best: "ev-ok", mixed: "ev-warn", blunder: "ev-bad" };
const freqPct = (x) => (x === null || x === undefined ? "–" : `${(100 * x).toFixed(0)}%`);

function gradeChip(row) {
  if (!row) return null;
  if (!GRADE_CLS[row.grade]) {
    const why = row.note || (row.loss !== null && row.loss !== undefined ? "graded before Plan 7, run pn ev run again" : "unknown");
    const title = `not graded (${row.source}): ${why}`;
    return h("span", { class: "ev-chip ev-na", title, "aria-label": title }, "not graded");
  }
  const title = `${row.grade}: solver plays ${row.chosen} ${freqPct(row.freq_chosen)}, ` +
    `most frequent ${row.best} ${freqPct(row.best_freq)} · source ${row.source}`;
  if (row.note === "combo_added_to_range") {
    const t = `hero's hand was outside the modeled range; the grade is unreliable (${title})`;
    return h("span", { class: "ev-chip ev-na", title: t, "aria-label": t }, `off-model ${row.grade} ${freqPct(row.freq_chosen)}`);
  }
  const best = row.best && row.best !== row.chosen ? ` · best ${row.best} ${freqPct(row.best_freq)}` : "";
  return h("span", { class: `ev-chip ${GRADE_CLS[row.grade]}`, title, "aria-label": title },
    `${row.grade} ${freqPct(row.freq_chosen)}${best}`);
}

export async function handView({ hand_id }, q) {
  const source = (q && q.get("source")) || "gto";
  const [r, ev] = await Promise.all([
    api(`/api/hands/${encodeURIComponent(hand_id)}`),
    api(`/api/hands/${encodeURIComponent(hand_id)}/ev?source=${encodeURIComponent(source)}`).catch(() => ({ rows: [] })),
  ]);
  const evBySeq = new Map(ev.rows.map((row) => [row.seq, row]));
  const hand = r.hand;
  const bb = (hand.blinds || {}).bb || 0;
  const name = (pid) => r.names[pid] || pid;
  const seats = table(["seat", "pos", "player", "stack", "cards", "net"].map((t, i) =>
    h("th", { scope: "col", class: [0, 3, 5].includes(i) ? "num" : null }, t)),
    hand.players.map((p) => h("tr", {},
      h("td", { class: "num" }, p.seat), h("td", {}, h("span", { class: "pos-tag" }, p.position)),
      h("th", { scope: "row" }, link(`/players/${encodeURIComponent(r.canonical[p.player_id])}`, name(p.player_id))),
      h("td", { class: "num" }, bbs(p.stack_start, bb)),
      h("td", {}, p.cards ? cards(p.cards) : h("span", { class: "muted" }, "—")),
      h("td", { class: `num ${signClass((hand.net || {})[p.player_id])}` }, bbs((hand.net || {})[p.player_id] || 0, bb)))));
  const streets = r.replay.map((st) => h("section", { class: "replay-street" },
    h("header", { class: "street-head" }, h("h3", {}, st.street), cards(st.cards),
      h("span", { class: "muted" }, `pot ${bbs(st.pot_start, bb)}`)),
    st.actions.length ? h("ol", { class: "actions" }, st.actions.map((a) => h("li", { class: `act act-${a.kind}` },
      h("span", { class: "who" }, name(a.player_id)),
      h("span", { class: "what" }, a.kind.replace("_", " "), a.amount ? ` ${bbs(a.amount, bb)}` : "",
        a.kind === "raise" || a.kind === "bet" ? h("span", { class: "muted" }, ` to ${bbs(a.to_amount, bb)}`) : null,
        a.allin ? h("span", { class: "badge" }, "all-in") : null, gradeChip(evBySeq.get(a.seq))),
      h("span", { class: "num muted" }, `pot ${bbs(a.pot_after, bb)} · stack ${bbs(a.stack_after, bb)}`))))
      : h("p", { class: "muted" }, "no actions")));
  const pots = (hand.pots || []).map((p) => h("li", {}, `${bbs(p.amount, bb)} → `,
    (p.winners || []).map((w) => name(w.player_id)).join(", ")));
  return h("div", { class: "page" },
    pageHead("Hand replay", `Hand #${hand.hand_no}`,
      `${hand.variant || "NLHE"} · blinds ${chips((hand.blinds || {}).sb)}/${chips(bb)} · ${hand.started_at || ""} `,
      (hand.flags || []).map((f) => h("span", { class: "badge" }, f))),
    h("div", { class: "board-strip" }, h("span", { class: "label" }, "Board"), boardCards(hand.board || {}),
      hand.board && hand.board.flop && hand.board.flop.length ? null : h("span", { class: "muted" }, "no flop")),
    section("Seats", seats),
    section("Action", ...streets),
    pots.length ? section("Pots", h("ul", { class: "pots" }, pots)) : null);
}

