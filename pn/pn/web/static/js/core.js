// Shared helpers: safe DOM building, API fetch, formatting, router.

export const RANKS = "AKQJT98765432";

/** h("div", {class: "x", onclick: fn}, child, "text", [more]) -> Element. Text is never parsed as HTML. */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    if (Array.isArray(c)) append(el, c);
    else el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export class ApiError extends Error {}

export async function api(path, opts) {
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch { /* non-JSON error body */ }
  if (!res.ok) throw new ApiError((body && body.detail) || `${res.status} ${res.statusText}`);
  return body;
}

export const pct = (x, d = 0) => (x === null || x === undefined ? "–" : `${(100 * x).toFixed(d)}%`);
export const num = (x, d = 2) => (x === null || x === undefined ? "–" : Number(x).toFixed(d));
export const money = (cents) => {
  if (cents === null || cents === undefined) return "–";
  const v = cents / 100;
  return `${v > 0 ? "+" : ""}${v.toFixed(2)}`;
};
export const isAf = (stat) => stat.startsWith("af_");
export const fmtStat = (stat, v, d = 0) => (isAf(stat) ? num(v, 2) : pct(v, d));
export const signClass = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");

const SUIT_GLYPH = { s: "♠", h: "♥", d: "♦", c: "♣" };
export function card(c) {
  if (!c) return h("span", { class: "card card-blank" }, "?");
  return h("span", { class: `card suit-${c[1]}`, title: c }, c[0], SUIT_GLYPH[c[1]] || c[1]);
}
export const cards = (list) => h("span", { class: "cards" }, (list || []).map(card));

export function loading(msg = "Loading…") {
  return h("p", { class: "loading" }, msg);
}

export function errorBox(e) {
  return h("div", { class: "error", role: "alert" }, h("strong", {}, "Error: "), e.message || String(e));
}

/** All 169 classes in 13x13 grid order: row i, col j. Pairs on the diagonal, suited above. */
export function gridClass(i, j) {
  const a = RANKS[i], b = RANKS[j];
  if (i === j) return a + b;
  return i < j ? a + b + "s" : b + a + "o";
}

// ------------------------------------------------------------------ tooltip
const tipEl = () => document.getElementById("tip");
export function showTip(target, content) {
  const tip = tipEl();
  tip.replaceChildren(content);
  tip.hidden = false;
  const r = target.getBoundingClientRect();
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  let x = r.right + 10, y = r.top;
  if (x + tw > window.innerWidth - 8) x = Math.max(8, r.left - tw - 10);
  if (y + th > window.innerHeight - 8) y = Math.max(8, window.innerHeight - th - 8);
  tip.style.transform = `translate(${x}px, ${y}px)`;
}
export function hideTip() { tipEl().hidden = true; }

// ------------------------------------------------------------------ router
const routes = [];
export function route(pattern, render, nav) {
  const keys = [];
  const re = new RegExp("^" + pattern.replace(/:(\w+)/g, (_, k) => { keys.push(k); return "([^/]+)"; }) + "/?$");
  routes.push({ re, keys, render, nav });
}

export function navigate(url, replace = false) {
  if (replace) history.replaceState(null, "", url);
  else history.pushState(null, "", url);
  dispatch();
}

/** Update the query string without re-rendering (shareable state). */
export function setQuery(params) {
  const u = new URL(location.href);
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === undefined || v === "") u.searchParams.delete(k);
    else u.searchParams.set(k, v);
  }
  history.replaceState(null, "", u);
}

export async function dispatch() {
  const app = document.getElementById("app");
  const path = location.pathname;
  const q = new URLSearchParams(location.search);
  hideTip();
  for (const r of routes) {
    const m = path.match(r.re);
    if (!m) continue;
    const params = Object.fromEntries(r.keys.map((k, i) => [k, decodeURIComponent(m[i + 1])]));
    document.querySelectorAll("[data-nav]").forEach((a) =>
      a.toggleAttribute("aria-current", a.dataset.nav === r.nav));
    app.replaceChildren(loading());
    try {
      const view = await r.render(params, q);
      app.replaceChildren(view);
    } catch (e) {
      app.replaceChildren(errorBox(e));
    }
    return;
  }
  app.replaceChildren(errorBox(new Error(`No page at ${path}`)));
}

export function startRouter() {
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-link]");
    if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    navigate(a.getAttribute("href"));
    document.getElementById("app").focus({ preventScroll: true });
    window.scrollTo(0, 0);
  });
  window.addEventListener("popstate", dispatch);
  dispatch();
}

export const link = (href, ...children) => h("a", { href, "data-link": true }, ...children);

// ------------------------------------------------------------------ toast, shortcuts sheet, keys
const TOAST_MS = 2200;
let toastEl = null, toastTimer = 0;
/** toast("Saved") -> bottom-centre status line, auto-hides after 2.2 s; a new toast replaces the current one. */
export function toast(msg) {
  if (!toastEl) {
    toastEl = h("div", { class: "toast", role: "status", "aria-live": "polite", "aria-atomic": "true" });
    document.body.append(toastEl);
  }
  clearTimeout(toastTimer);
  toastEl.classList.remove("is-on");
  toastEl.textContent = String(msg);
  void toastEl.offsetWidth; // restart the enter animation
  toastEl.classList.add("is-on");
  toastTimer = setTimeout(() => toastEl.classList.remove("is-on"), TOAST_MS);
}

let sheetClose = null;
/** shortcutsSheet([{title, rows: [[keys, desc]]}]) -> close(). keys: "N" or ["N", "Space"] (one kbd each).
 *  Modal; click outside, Esc or the ESC button closes; focus returns to where it was. */
export function shortcutsSheet(sections) {
  if (sheetClose) sheetClose();
  const back = document.activeElement;
  const btn = h("button", { type: "button", class: "sheet-close", "aria-label": "Close shortcuts" }, "Esc");
  const body = (sections || []).map((s) => h("section", { class: "sheet-sec" },
    s.title ? h("h2", { class: "sheet-title" }, s.title) : null,
    h("dl", { class: "sheet-rows" }, (s.rows || []).map(([keys, desc]) => [
      h("dt", {}, (Array.isArray(keys) ? keys : [keys]).map((k) => h("kbd", {}, k))),
      h("dd", {}, desc)]))));
  const dialog = h("div", { class: "sheet", role: "dialog", "aria-modal": "true", "aria-label": "Keyboard shortcuts" },
    h("div", { class: "sheet-head" }, h("span", { class: "sheet-kicker" }, "Shortcuts"), btn), body);
  const scrim = h("div", { class: "sheet-scrim" }, dialog);
  const onKey = (e) => {
    if (e.key === "Escape" || e.key === "?") { e.preventDefault(); e.stopImmediatePropagation(); close(); }
    else if (e.key === "Tab") { e.preventDefault(); btn.focus(); } // single focusable: keep focus inside
  };
  function close() {
    if (sheetClose !== close) return;
    sheetClose = null;
    document.removeEventListener("keydown", onKey, true);
    scrim.remove();
    if (back && back.isConnected) back.focus({ preventScroll: true });
  }
  scrim.addEventListener("click", (e) => { if (e.target === scrim) close(); });
  btn.addEventListener("click", close);
  document.addEventListener("keydown", onKey, true);
  document.body.append(scrim);
  sheetClose = close;
  btn.focus({ preventScroll: true });
  return close;
}

/** onKeys((e) => ..., root?) -> unregister(). Document keydown, skipped while typing in a field, with
 *  Cmd/Ctrl/Alt held, or while the shortcuts sheet is open. With `root`, it also unregisters itself on the
 *  first key after root leaves the DOM (the router replaces views without a teardown hook); keys pressed
 *  before root is first inserted are ignored, not treated as a teardown. */
export function onKeys(handler, root) {
  let seen = false;  // root may not be inserted yet while the router awaits the view's render
  const onKey = (e) => {
    if (root) {
      if (root.isConnected) seen = true;
      else if (seen) { off(); return; }
      else return;
    }
    if (sheetClose || e.metaKey || e.ctrlKey || e.altKey) return;
    const t = e.target;
    if (t instanceof Element && (t.closest("input, select, textarea") || t.isContentEditable)) return;
    handler(e);
  };
  const off = () => document.removeEventListener("keydown", onKey);
  document.addEventListener("keydown", onKey);
  return off;
}
