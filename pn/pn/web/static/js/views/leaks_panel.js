// Train side column: the spots with the most expected blunders from /api/train/leaks (contracts/trainer.md,
// Plan 6 + Plan 7 frequency grades), top rows as the API sorts them. The hand's drilled spot gets the accent swatch once the hand is over,
// never while hero is acting (setup promises that drill hands are not labelled during play).
import { h, api, num, pct } from "../core.js";

const LEAKS_TOP = 8;
const LEAKS_WINDOW = 1000;

/** {el, refresh(state), mark(state)}: refresh refetches (session start, hand end); mark only re-marks. */
export function leaksPanel() {
  const body = h("div", { class: "leaks-side-body" }, h("p", { class: "loading" }, "Loading leaks…"));
  const el = h("section", { class: "leaks-side", "aria-labelledby": "leaks-side-title" },
    h("div", { class: "head" }, h("h3", { class: "label", id: "leaks-side-title" }, "Leaks"),
      h("span", { class: "label" }, `last ${LEAKS_WINDOW} hands`)),
    body);
  let rows = null;
  let seq = 0;
  let drilled = null; // the spot shown as drilled (only after the hand)
  let seen = null; // {hand, drill}: the hand's last non-null drill (the server clears it when the hand leaves the line)

  function shownDrill(state) {
    if (state.drill) seen = { hand: state.hand_no, drill: state.drill };
    return state.hand_over && seen && seen.hand === state.hand_no ? seen.drill : null;
  }

  function draw() {
    if (rows instanceof Error) {
      body.replaceChildren(h("p", { class: "note" }, `Leaks unavailable: ${rows.message}`));
      return;
    }
    if (!rows || !rows.length) {
      body.replaceChildren(h("p", { class: "note" }, "No spots yet. Finish a hand to start the table."));
      return;
    }
    const head = h("li", { class: "cols", "aria-hidden": "true" }, h("span", {}), h("span", { class: "leak-label" }, "Spot"),
      h("span", { class: "num" }, "Freq"), h("span", { class: "num" }, "Blund"), h("span", { class: "num" }, "/100"), h("span", { class: "num" }, "n"));
    body.replaceChildren(h("ol", { class: "leaks-side-list" }, head, rows.slice(0, LEAKS_TOP).map((l) => {
      const here = drilled !== null && (l.spot === drilled.spot || l.label === drilled.label);
      return h("li", { class: here ? "is-drilled" : null, title: l.spot },
        h("span", { class: "swatch", "aria-hidden": "true" }),
        h("span", { class: "leak-label" }, l.label || l.spot, here ? h("span", { class: "visually-hidden" }, " (last hand's drill)") : null),
        h("span", { class: "num", title: "how often you reach this spot (solver reach)" }, pct(l.freq, l.freq !== null && l.freq < 0.1 ? 1 : 0)),
        h("span", { class: "num", title: `blunder rate (${l.blunders} of ${l.n} graded, shrunk toward the overall rate)` }, pct(l.blunder_rate)),
        h("span", { class: "num per100", title: "expected blunders per 100 hands" }, num(l.expected_blunders_per_100, 1)),
        h("span", { class: "num n", title: "graded decisions in this spot" }, l.n));
    })));
  }

  async function refresh(state) {
    const mine = ++seq;
    drilled = shownDrill(state);
    const q = new URLSearchParams({ window: LEAKS_WINDOW, table: state.table, depth: state.depth });
    try { rows = await api(`/api/train/leaks?${q}`); } catch (e) { rows = e; }
    if (mine === seq) draw();
  }

  function mark(state) {
    const d = shownDrill(state);
    if ((d && d.spot) === (drilled && drilled.spot)) return;
    drilled = d;
    if (rows) draw();
  }

  return { el, refresh, mark };
}
