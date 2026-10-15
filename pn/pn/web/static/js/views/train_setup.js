// Trainer setup (/train without sid): segmented table / depth / hero seat, opponents by real position,
// drill share presets and a one-line summary next to Deal. Enter deals from anywhere but a button.
import { h, api, onKeys, shortcutsSheet } from "../core.js";
import { pageHead, section } from "./ui.js";
import { SEATS, loadSetup, saveSetup, TRAIN_KEYS } from "./train_util.js";
import { savedParams } from "./leaks_panel.js";

const DRILL_DEFAULT = 0.8;
const DRILL_PRESETS = [[0, "off"], [50, "50%"], [80, "80%"], [100, "100%"]];
const MIXED = "__mixed";
const SLOW_DEAL_S = 5; // after this many seconds the Dealing status counts up

/** Segmented control: {el, value()}. opts = [[value, text]]; onPick(value) after a click. */
function seg(id, label, opts, value, onPick) {
  let cur = opts.some(([v]) => String(v) === String(value)) ? value : opts[0][0];
  const btns = opts.map(([v, t]) => h("button", { type: "button", class: "seg-opt", "aria-pressed": String(String(v) === String(cur)),
    onclick: () => {
      cur = v;
      btns.forEach((b, k) => b.setAttribute("aria-pressed", String(String(opts[k][0]) === String(cur))));
      onPick(v);
    } }, t));
  const el = h("div", { class: "field" }, h("span", { id }, label),
    h("div", { class: `seg${opts.length > 4 ? " seg-wrap" : ""}`, role: "group", "aria-labelledby": id }, btns));
  return { el, value: () => cur };
}

export async function setupView(onStart, notice) {
  let players = [];
  try { players = await api("/api/train/players"); } catch { /* no DB: GTO bots only */ }
  const saved = loadSetup();
  const oppOptions = [["", "GTO bot"], ...players.map((p) => [p.player_id, `${p.name} · ${p.hands} hands`])];
  const select = (id, options, value) => h("select", { id },
    options.map(([v, t]) => h("option", { value: v, selected: String(v) === String(value) }, t)));

  let opps = (saved.opponents || []).map((v) => v || "");
  const tableSeg = seg("tr-table", "Table", [[6, "6-max"], [8, "8-max"]], saved.table || 6, () => { drawHero(); drawOpps(); summarize(); });
  const depthSeg = seg("tr-depth", "Depth", [[100, "100 bb"], [200, "200 bb"]], saved.depth || 100, summarize);
  const heroHolder = h("div", { class: "hero-seat-holder" });
  let heroSeg = null;
  function drawHero() {
    const n = +tableSeg.value();
    const prev = heroSeg ? heroSeg.value() : saved.hero_seat || "";
    heroSeg = seg("tr-hero", "Hero seat", [["", "Random"], ...SEATS[n].map((p) => [p, p])], SEATS[n].includes(prev) ? prev : "",
      () => { drawOpps(); summarize(); });
    heroHolder.replaceChildren(heroSeg.el);
  }

  const rngBox = h("input", { type: "checkbox", id: "tr-rng", checked: saved.rng_enabled !== false, onchange: () => summarize() });
  const drillPct0 = Math.round(100 * (typeof saved.drill_share === "number" ? saved.drill_share : DRILL_DEFAULT));
  const drillOut = h("output", { for: "tr-drill", class: "drill-out" }, `${drillPct0}%`);
  const drillRange = h("input", { type: "range", id: "tr-drill", min: 0, max: 100, step: 5, value: drillPct0,
    "aria-describedby": "tr-drill-note", oninput: () => drillChanged() });
  const presets = DRILL_PRESETS.map(([v, t]) => h("button", { type: "button", class: "chip drill-chip-opt",
    onclick: () => { drillRange.value = v; drillChanged(); } }, t));
  function drillChanged() {
    drillOut.textContent = `${drillRange.value}%`;
    presets.forEach((b, k) => b.setAttribute("aria-pressed", String(+drillRange.value === DRILL_PRESETS[k][0])));
    summarize();
  }

  const oppGrid = h("div", { class: "train-opps" });
  const bulk = h("select", { id: "tr-opp-all", onchange: () => {
    if (bulk.value === MIXED) return;
    opps = opps.map(() => bulk.value);
    drawOpps();
    summarize();
  } });
  function drawOpps() {
    const n = +tableSeg.value();
    const hero = heroSeg ? heroSeg.value() : "";
    const order = SEATS[n];
    const at = order.indexOf(hero);
    opps = Array.from({ length: n - 1 }, (_, k) => opps[k] || "");
    oppGrid.replaceChildren(...opps.map((v, k) => {
      const pos = at >= 0 ? order[(at + k + 1) % n] : `Seat ${k + 1}`;
      const s = select(`tr-opp-${k}`, oppOptions, v);
      s.addEventListener("change", () => { opps[k] = s.value; syncBulk(); summarize(); });
      return h("label", { class: "opp-row", for: `tr-opp-${k}` }, h("span", { class: "opp-pos" }, pos), s);
    }));
    syncBulk();
  }
  function syncBulk() {
    const same = opps.every((v) => v === opps[0]);
    bulk.replaceChildren(...[...(same ? [] : [[MIXED, "mixed"]]), ...oppOptions].map(([v, t]) => h("option", { value: v }, t)));
    bulk.value = same ? opps[0] || "" : MIXED;
  }

  const summary = h("span", { class: "setup-summary", "aria-live": "polite" });
  function summarize() {
    const named = opps.filter(Boolean).length;
    const bots = opps.length - named;
    const hero = heroSeg ? heroSeg.value() : "";
    summary.textContent = [`${tableSeg.value()}-max`, `${depthSeg.value()} bb`, `hero ${hero || "random"}`,
      `${bots} GTO bot${bots === 1 ? "" : "s"}${named ? ` + ${named} named` : ""}`, `RNG ${rngBox.checked ? "on" : "off"}`,
      +drillRange.value ? `drill ${drillRange.value}%` : "drill off"].join(" · ");
  }

  drawHero();
  drawOpps();
  drillChanged();

  const status = h("p", { class: "warn", role: "status" }, notice || "");
  const btn = h("button", { type: "submit", class: "btn btn-deal" }, "Deal", h("kbd", { class: "kbd-inv", "aria-hidden": "true" }, "↵"));
  const form = h("form", { class: "train-setup", onsubmit: async (e) => {
    e.preventDefault();
    if (btn.disabled) return;
    const body = { table: +tableSeg.value(), depth: +depthSeg.value(), source: "gto", // the only trainer source (Plan 7)
      hero_seat: heroSeg.value() || null, opponents: opps.map((v) => v || null),
      rng_enabled: rngBox.checked, drill_share: +drillRange.value / 100 };
    saveSetup(body);
    Object.assign(body, savedParams()); // the importance tab's p / pot (Plan 14); kept in pn.train.progress, not the setup
    btn.disabled = true;
    status.textContent = "Dealing…";
    // a deal is normally instant: a slow one (a busy server) still shows it is alive
    const t0 = Date.now();
    const tick = setInterval(() => {
      if (!status.isConnected) return clearInterval(tick);
      const s = Math.round((Date.now() - t0) / 1000);
      if (s >= SLOW_DEAL_S) status.textContent = `Dealing… ${s} s`;
    }, 1000);
    try { onStart(await api("/api/train/new", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) })); }
    catch (err) { status.textContent = err.message; btn.disabled = false; }
    finally { clearInterval(tick); }
  } },
    h("div", { class: "filters setup-segs" }, tableSeg.el, depthSeg.el, heroHolder),
    h("div", { class: "setup-row" },
      h("label", { class: "check-field", for: "tr-rng" }, rngBox, h("span", {}, "RNG for mixed spots")),
      h("div", { class: "field drill-field" },
        h("span", { class: "drill-head" }, h("label", { for: "tr-drill" }, "Drill share"), drillOut),
        drillRange,
        h("div", { class: "chips", role: "group", "aria-label": "Drill share presets" }, presets),
        h("p", { class: "note", id: "tr-drill-note" }, "Share of hands dealt into the spots where you blunder most (see Leaks). Drill hands are not labelled while you play."))),
    h("div", { class: "setup-opps" },
      h("div", { class: "setup-opps-head" }, h("span", { class: "label" }, "Opponents · clockwise from hero"),
        h("label", { class: "opp-bulk", for: "tr-opp-all" }, "Set every seat to", bulk)),
      oppGrid,
      h("p", { class: "note" }, "A named player only labels the seat: every villain plays the GTO strategy (charts preflop, solver postflop).")),
    h("div", { class: "setup-deal" }, btn, summary, status));
  // range and checkbox inputs swallow Enter (onKeys skips inputs): deal from them too
  form.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.matches && e.target.matches("input[type=range], input[type=checkbox]")) { e.preventDefault(); form.requestSubmit(); }
  });

  const root = h("div", { class: "page" },
    pageHead("Trainer", "Play", "Six or eight handed against GTO bots. Every decision is graded by the solver's frequency for your action. ",
      h("a", { href: "/train/performance", "data-link": true, class: "pf-link" }, "Performance by spot")),
    section("New session", form));
  onKeys((e) => {
    if (e.key === "?") { e.preventDefault(); shortcutsSheet(TRAIN_KEYS); return; }
    if (e.key !== "Enter" || !root.isConnected) return;
    if (e.target instanceof Element && e.target.closest("button, a, summary")) return; // a focused button keeps Enter
    e.preventDefault();
    form.requestSubmit();
  }, root);
  return root;
}
