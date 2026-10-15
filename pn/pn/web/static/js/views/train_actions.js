// Trainer action bar (buttons with pressed feedback, wait line, Next hand + Auto-deal countdown), the inline
// verdict under it and the first-decision hint. Pure builders: train.js owns the state and timers.
import { h, pct } from "../core.js";
import { actionColors } from "../grid.js";
import { bb, toBB, tokenName, optLabel, shortcutFor, heroToAct, heroLast } from "./train_util.js";
import { GRADE_LEVEL, verdictParts } from "./train_feedback.js";

const wait = (text) => h("p", { class: "train-wait" }, h("span", { class: "dot", "aria-hidden": "true" }), text);

/**
 * Bar children for the current state. ui: {busy, pressed (token|null), solvingSecs, autoDeal, countdown (s left|null),
 * countdownTotal, onAct(token), onNext(), onAuto(on)} -> {key, nodes, label (Next-hand text node holder|null)}.
 * `key` changes only when the bar must be rebuilt (the countdown ticks by editing `label`).
 */
export function barContent(state, ui) {
  if (state.hand_over) {
    const counting = ui.countdown !== null;
    const label = h("span", { class: "next-label" }, counting ? `Next hand in ${ui.countdown}s` : "Next hand");
    const btn = h("button", { type: "button", class: `btn train-next${counting ? " is-counting" : ""}`, disabled: ui.busy || state.solving,
      onclick: ui.onNext, "aria-keyshortcuts": "N Space" },
      counting ? h("span", { class: "next-drain", style: `--cd: ${ui.countdownTotal}s`, "aria-hidden": "true" }) : null,
      label, h("kbd", { class: "kbd-inv", "aria-hidden": "true" }, "N"), h("kbd", { class: "kbd-inv", "aria-hidden": "true" }, "Space"));
    const auto = h("label", { class: "auto-deal", title: "Deal the next hand automatically after a short pause" },
      h("input", { type: "checkbox", checked: ui.autoDeal, onchange: (e) => ui.onAuto(e.currentTarget.checked) }), "Auto-deal");
    return { key: `over|${ui.busy}|${state.solving}|${counting}|${ui.autoDeal}|${state.hand_no}`, label,
      nodes: [h("div", { class: "train-next-row" }, btn, auto)] };
  }
  const you = heroLast(state);
  const prefix = you ? `you: ${you} · ` : "";
  if (state.solving) {
    const text = `${prefix}solving the ${state.street}… ${ui.solvingSecs}s`;
    return { key: `solve|${text}`, label: null, nodes: [wait(text)] };
  }
  if (!heroToAct(state)) {
    const text = `${prefix}${state.to_act || "table"} to act…`;
    return { key: `wait|${text}`, label: null, nodes: [wait(text)] };
  }
  const legal = state.legal_actions;
  const keys = shortcutFor(legal);
  const letter = Object.fromEntries(Object.entries(keys).map(([k, i]) => [i, k]));
  const colors = actionColors(legal.map((a) => a.token));
  const nodes = legal.map((a, k) => {
    const isPressed = ui.pressed === a.token;
    const cls = ["action-btn", isPressed && "is-pressed", ui.pressed && !isPressed && "is-dimmed"].filter(Boolean).join(" ");
    return h("button", { type: "button", class: cls, style: `--act: ${colors[k]}`, disabled: ui.busy, "aria-pressed": isPressed ? "true" : null,
      onclick: () => ui.onAct(a.token), "aria-keyshortcuts": [letter[k], k < 9 ? String(k + 1) : null].filter(Boolean).join(" ") },
      h("span", { class: "act-name" }, a.label || tokenName(a.token), // server labels already carry the amount
        !a.label && a.amount !== null && a.amount !== undefined ? h("span", { class: "act-sub" }, `${bb(toBB(state, a.amount))} bb`) : null),
      h("kbd", {}, (letter[k] || (k < 9 ? String(k + 1) : "")).toUpperCase()));
  });
  return { key: `act|${state.hand_no}|${state.history.length}|${legal.map((a) => a.token).join(",")}|${ui.busy}|${ui.pressed}`, label: null, nodes };
}

/** Inline verdict for the latest graded decision. opts {canRetry, onRetry(), onWhy()}. */
export function verdictEl(fb, opts) {
  const v = verdictParts(fb);
  const lvl = GRADE_LEVEL[fb.grade] || "mute";
  const top = fb.best === fb.chosen;
  const detail = !GRADE_LEVEL[fb.grade] ? v.best
    : top ? `solver plays this ${pct(fb.freq)}, its top action`
      : `solver ${pct(fb.freq)} · best ${optLabel(fb, fb.best)} ${pct(fb.best_freq)}`;
  return h("div", { class: `train-verdict lvl-${lvl}`, role: "status" },
    h("span", { class: "verdict-status" }, v.status),
    h("span", { class: "verdict-chosen" }, optLabel(fb, fb.chosen)),
    h("span", { class: "verdict-detail" }, detail),
    fb.retry ? h("span", { class: "badge" }, "retry · not counted") : null,
    h("span", { class: "verdict-tools" },
      opts.canRetry ? h("button", { type: "button", class: "verdict-btn is-retry", onclick: opts.onRetry, "aria-keyshortcuts": "Z",
        title: "Replay the decision before this verdict. Retries are not counted." }, "Retry spot", h("kbd", { class: "kbd-soft", "aria-hidden": "true" }, "Z")) : null,
      h("button", { type: "button", class: "verdict-btn", onclick: opts.onWhy }, "Why →")));
}

export const hintEl = () => h("p", { class: "train-hint" }, "Press the letter on a button, or ",
  h("kbd", { class: "kbd-soft" }, "1"), "–", h("kbd", { class: "kbd-soft" }, "9"), " in order. ",
  h("kbd", { class: "kbd-soft" }, "?"), " lists every shortcut.");
