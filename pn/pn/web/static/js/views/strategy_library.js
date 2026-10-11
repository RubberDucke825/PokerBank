// Saved solves (/strategy/library): every cached solve with board, line, pot type, depth, exploitability
// and date; filters by street / pot type / texture / position; open in the browser; delete.
import { h, api, num, cards, link, setQuery, navigate } from "../core.js";
import { texture } from "../deck.js";
import { pageHead, empty } from "./ui.js";
import { postJson } from "./strategy_jobs.js";

const TEXTURES = ["monotone", "two-tone", "rainbow", "paired", "connected", "A-high", "K-high", "Q-high", "J/T-high", "low"];
const POT_LABEL = { limped: "Limped", srp: "SRP", "3bp": "3-bet", "4bp+": "4-bet+" };

function ago(iso) {
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** Exploitability as % pot with a quality class (good <= 0.5, warn <= 1.5, else bad). */
function expl(v) {
  if (v === null || v === undefined) return h("span", { class: "x-na" }, "–");
  const cls = v <= 0.5 ? "x-good" : v <= 1.5 ? "x-warn" : "x-bad";
  return h("span", { class: cls }, `${num(v, v < 10 ? 2 : 0)}%`);
}

const matchup = (r) => {
  const [oop, ip] = r.positions;
  return h("span", { class: "lib-match" }, h("b", {}, ip), h("span", { class: "muted" }, " vs "), h("b", {}, oop));
};

export async function strategyLibraryView(_params, q) {
  let rows = await api("/api/solves/library");
  const f = { street: q.get("street") || "all", pot: q.get("pot") || "all", tex: q.get("tex") || "", pos: q.get("pos") || "" };
  const list = h("div", { class: "lib-list", role: "list" });
  const count = h("span", { class: "muted mono" });
  const bar = h("div", { class: "lib-filters" });

  const segBtns = (key, opts) => h("div", { class: "seg", role: "group", "aria-label": key },
    opts.map(([v, t]) => h("button", { type: "button", class: "seg-opt", "aria-pressed": String(f[key] === v),
      onclick: () => { f[key] = v; sync(); } }, t)));

  function sync() {
    setQuery({ street: f.street === "all" ? null : f.street, pot: f.pot === "all" ? null : f.pot, tex: f.tex || null, pos: f.pos || null });
    drawBar();
    drawList();
  }

  function drawBar() {
    const positions = [...new Set(rows.flatMap((r) => r.positions))];
    bar.replaceChildren(
      h("div", { class: "field" }, h("span", {}, "Street"), segBtns("street", [["all", "All"], ["flop", "Flop"], ["turn", "Turn"], ["river", "River"]])),
      h("div", { class: "field" }, h("span", {}, "Pot"), segBtns("pot", [["all", "All"], ["srp", "SRP"], ["3bp", "3-bet"], ["4bp+", "4-bet+"]])),
      h("label", { class: "field", for: "lib-pos" }, h("span", {}, "Position"),
        h("select", { id: "lib-pos", onchange: (e) => { f.pos = e.target.value; sync(); } },
          h("option", { value: "" }, "Any"), positions.map((p) => h("option", { value: p, selected: p === f.pos }, p)))),
      h("div", { class: "field tex-field" }, h("span", {}, "Board texture"),
        h("div", { class: "chips", role: "group", "aria-label": "Board texture" }, TEXTURES.map((t) =>
          h("button", { type: "button", class: "chip", "aria-pressed": String(f.tex === t),
            onclick: () => { f.tex = f.tex === t ? "" : t; sync(); } }, t)))));
  }

  const keep = (r) => (f.street === "all" || r.street === f.street) && (f.pot === "all" || r.pot_type === f.pot)
    && (!f.tex || texture(r.board).includes(f.tex)) && (!f.pos || r.positions.includes(f.pos));

  function row(r) {
    const href = `/strategy/${encodeURIComponent(r.name)}`;
    let armed = false;
    const del = h("button", { type: "button", class: "lib-del", "aria-label": `Delete ${r.board.join("")} ${r.positions.join(" vs ")}`,
      onclick: async (e) => {
        e.stopPropagation();
        if (!armed) { armed = true; del.textContent = "Confirm"; del.classList.add("armed"); setTimeout(() => { armed = false; del.textContent = "Delete"; del.classList.remove("armed"); }, 3000); return; }
        del.disabled = true;
        try {
          await postJson(`/api/solves/${encodeURIComponent(r.name)}`, undefined, "DELETE");
          rows = rows.filter((x) => x.key !== r.key);
          drawList();
        } catch (err) { del.disabled = false; del.textContent = "Failed"; del.title = err.message; }
      } }, "Delete");
    const sub = r.parent ? `subgame of ${r.parent.path.join(" ") || "root"}` : (POT_LABEL[r.pot_type] || r.pot_type);
    return h("div", { class: "lib-row", role: "listitem", onclick: (e) => { if (!e.target.closest("a,button")) navigate(href); } },
      h("div", { class: "lib-board" }, cards(r.board)),
      h("div", { class: "lib-main" },
        link(href, matchup(r)),
        h("span", { class: "lib-sub" }, h("span", { class: `street-tag st-${r.street}` }, r.street), ` ${sub} · ${r.depth_bb} bb · pot ${num(r.pot_bb, 1)} bb`)),
      h("div", { class: "lib-num" }, h("span", { class: "label" }, "Exploit."), expl(r.exploitability_pct_pot)),
      h("div", { class: "lib-num hide-sm" }, h("span", { class: "label" }, "Iter."), h("span", {}, r.iterations ?? "–")),
      h("div", { class: "lib-num hide-sm" }, h("span", { class: "label" }, "Engine"), h("span", {}, r.backend || "–")),
      h("div", { class: "lib-date" }, h("time", { datetime: r.date, title: r.date }, ago(r.date))),
      del);
  }

  function drawList() {
    const shown = rows.filter(keep);
    count.textContent = `${shown.length} of ${rows.length}`;
    list.replaceChildren(...(shown.length ? shown.map(row)
      : [empty(rows.length ? "No saved solve matches these filters." : "No saved solves yet. Build a spot to start.")]));
  }

  drawBar();
  drawList();
  return h("div", { class: "page" },
    h("div", { class: "lib-head" },
      pageHead("Strategy", "Saved solves", "Heads-up postflop solves from TexasSolver, newest first. Open one to walk its tree."),
      h("a", { href: "/strategy/new", "data-link": true, class: "btn new-btn" }, "+ New spot")),
    h("section", { class: "panel lib-panel", "aria-label": "Saved solves" },
      h("div", { class: "lib-top" }, bar, count), list));
}
