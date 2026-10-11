// Home and players list.
import { h, api, link, money, fmtStat, signClass, setQuery } from "../core.js";
import { table, pageHead, section, devClass } from "./ui.js";

const LIST_STATS = [["vpip", "VPIP"], ["pfr", "PFR"], ["three_bet", "3bet"], ["af_flop", "AF flop"], ["wtsd", "WTSD"]];

function statTd(stat, s) {
  if (!s) return h("td", { class: "num empty-cell" }, "·");
  const obj = { dev_gto: s.gto === null || s.gto === undefined ? 0 : (s.ci90[0] > s.gto ? 1 : s.ci90[1] < s.gto ? -1 : 0), n: s.n };
  return h("td", { class: `num stat ${devClass(obj)}`, title: s.gto !== null && s.gto !== undefined ? `GTO ${fmtStat(stat, s.gto)}` : "" },
    h("span", { class: "v" }, fmtStat(stat, s.v)), h("span", { class: "n" }, ` ${s.n}`));
}

const SORTS = {
  hands: (p) => p.hands, net: (p) => p.net, name: (p) => p.name.toLowerCase(), archetype: (p) => p.archetype,
  ...Object.fromEntries(LIST_STATS.map(([k]) => [k, (p) => (p.stats[k] ? p.stats[k].v : -1)])),
};

function playersTable(players, sortKey, desc, onSort) {
  const th = (key, text, num = true) => h("th", {
    scope: "col", class: num ? "num" : "", "aria-sort": key === sortKey ? (desc ? "descending" : "ascending") : "none",
  }, h("button", { class: "sort", type: "button", onclick: () => onSort(key) }, text,
    key === sortKey ? h("span", { "aria-hidden": "true" }, desc ? " ↓" : " ↑") : null));
  const rows = players.map((p) => h("tr", {},
    h("th", { scope: "row" }, link(`/players/${encodeURIComponent(p.canonical_id)}`, p.name)),
    h("td", { class: "num" }, p.hands),
    h("td", { class: `num ${signClass(p.net)}` }, money(p.net)),
    LIST_STATS.map(([k]) => statTd(k, p.stats[k])),
    h("td", {}, h("span", { class: `arch arch-${p.archetype}` }, p.archetype.replaceAll("_", " "))),
    h("td", {}, link(`/review/${encodeURIComponent(p.canonical_id)}`, "review"))));
  return table([th("name", "player", false), th("hands", "hands"), th("net", "net"),
    ...LIST_STATS.map(([k, t]) => th(k, t)), th("archetype", "archetype", false), h("th", {}, "")], rows);
}

function sorted(players, key, desc) {
  const f = SORTS[key] || SORTS.hands;
  return [...players].sort((a, b) => {
    const x = f(a), y = f(b);
    const c = x < y ? -1 : x > y ? 1 : 0;
    return desc ? -c : c;
  });
}

export async function playersView(_params, q) {
  const players = await api("/api/players");
  let key = SORTS[q.get("sort")] ? q.get("sort") : "hands";
  let desc = q.get("dir") !== "asc";
  const holder = h("div", {});
  const draw = () => holder.replaceChildren(playersTable(sorted(players, key, desc), key, desc, (k) => {
    desc = k === key ? !desc : k !== "name" && k !== "archetype";
    key = k;
    setQuery({ sort: key, dir: desc ? "desc" : "asc" });
    draw();
  }));
  draw();
  return h("div", { class: "page" },
    pageHead("Every player, every hand", "Players", `${players.length} players · cells: posterior (opportunities); red/blue = 90% CI above/below GTO`),
    holder);
}

export async function homeView() {
  const players = await api("/api/players");
  const totalHands = players.reduce((m, p) => Math.max(m, p.hands), 0);
  const top = players.slice(0, 6);
  return h("div", { class: "page home" },
    h("header", { class: "hero-block" },
      h("span", { class: "label" }, "Local study desk"),
      h("h1", {}, "Read the table, ", h("em", {}, "then"), " read yourself."),
      h("p", { class: "lede" }, "Bayesian player profiles from your PokerNow hands, positional leaks against GTO Wizard baselines, hand replays, preflop charts and solver strategy grids.")),
    h("div", { class: "home-grid" },
      section("Most-seen players",
        h("ol", { class: "player-list" }, top.map((p) => h("li", {},
          link(`/players/${encodeURIComponent(p.canonical_id)}`, h("span", { class: "pl-name" }, p.name)),
          h("span", { class: "pl-meta" }, `${p.hands} hands · ${p.archetype.replaceAll("_", " ")}`),
          h("span", { class: `pl-net ${signClass(p.net)}` }, money(p.net))))),
        link("/players", "All players →")),
      h("nav", { class: "tiles", "aria-label": "Study tools" },
        tile("/train", "Train", "Play hands against GTO bots or modelled regulars, graded per decision."),
        tile("/strategy", "Strategy", "Walk a solver tree, hand-class grid per node."),
        tile("/hands", "Hands", "Filter, then replay street by street."),
        tile("/review", "Hero review", "Deviations judged against the opponents you faced.")),
      h("p", { class: "home-foot" }, `${players.length} players; the most active has ${totalHands} hands.`)));
}

const tile = (href, title, text) => h("a", { href, "data-link": true, class: "tile" },
  h("span", { class: "tile-key" }, href), h("span", {}, h("span", { class: "tile-title" }, title), h("br"), h("span", { class: "tile-text" }, text)));
