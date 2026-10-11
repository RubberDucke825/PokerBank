// Solve jobs for the Strategy tab: start + poll (/api/solves/jobs/{id}), the live progress panel
// (iterations, exploitability sparkline, elapsed, cancel) and the job error box.
import { h, api, num } from "../core.js";

const POLL_MS = 1000;
const SPARK_W = 240, SPARK_H = 56, SPARK_PAD = 4;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const gb = (b) => `${num(b / 1e9, 1)} GB`;

/** Error carrying the job (or error response) body; too_large estimates live there. */
export class JobError extends Error {
  constructor(message, body) { super(message); this.body = body || {}; }
}

export async function postJson(path, body, method = "POST") {
  const res = await fetch(path, { method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
  let out = null;
  try { out = await res.json(); } catch { /* non-JSON error body */ }
  if (!res.ok) {
    const d = out && out.detail;
    throw new JobError(typeof d === "string" ? d : d ? JSON.stringify(d) : `${res.status} ${res.statusText}`, out);
  }
  return out;
}

/**
 * POST a solve request (202 {job_id}) and poll its job every second until it ends.
 * `onUpdate(job)` gets every job view (status, iterations, exploitability_pct_pot, curve,
 * elapsed_sec). Resolves with the solve name; rejects with JobError ("cancelled" when cancelled
 * or once `alive()` turns false). `onStart(jobId)` lets the caller wire a cancel button.
 */
export async function runJob(path, body, { onUpdate = () => {}, onStart = () => {}, alive = () => true } = {}) {
  onUpdate({ status: "queued", elapsed_sec: 0, iterations: null, exploitability_pct_pot: null, curve: [] });
  const start = await postJson(path, body);
  onStart(start.job_id);
  for (;;) {
    if (!alive()) throw new JobError("cancelled", { status: "cancelled" });
    const j = await api(`/api/solves/jobs/${encodeURIComponent(start.job_id)}`);
    onUpdate(j);
    if (j.status === "done") return j.name;
    if (j.status === "cancelled") throw new JobError("cancelled", j);
    if (j.status === "error") throw new JobError(j.error || "solve failed", j);
    await sleep(POLL_MS);
  }
}

export const cancelJob = (jobId) => postJson(`/api/solves/jobs/${encodeURIComponent(jobId)}/cancel`, {});

export function jobErrorBox(e) {
  const b = (e && e.body) || {};
  if (b.status === "cancelled") return h("p", { class: "note", role: "status" }, "Solve cancelled.");
  if (b.error !== "too_large") return h("div", { class: "error", role: "alert" }, h("strong", {}, "Error: "), e.message || String(e));
  return h("div", { class: "error", role: "alert" },
    h("strong", {}, "Too large to solve here. "),
    b.estimate_bytes !== undefined ? `The tree needs about ${gb(b.estimate_bytes)}${b.limit_bytes ? ` against a ${gb(b.limit_bytes)} limit` : ""}. ` : "",
    "Build it once offline, then it loads instantly: ", h("code", {}, "pn library build"));
}

/** Exploitability curve (% pot by iteration) as an inline SVG; `target` draws the accuracy line. */
export function sparkline(curve, target) {
  const pts = (curve || []).filter((p) => p[2] !== null && p[2] !== undefined);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${SPARK_W} ${SPARK_H}`);
  svg.setAttribute("class", "spark job-spark");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", pts.length ? `Exploitability from ${num(pts[0][2], 2)}% to ${num(pts[pts.length - 1][2], 2)}% of the pot` : "No exploitability measured yet");
  const mk = (tag, attrs) => {
    const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
    svg.append(el);
    return el;
  };
  mk("line", { x1: 0, y1: SPARK_H - SPARK_PAD, x2: SPARK_W, y2: SPARK_H - SPARK_PAD, class: "axis" });
  if (pts.length < 2) return svg;
  // log scale: exploitability falls by orders of magnitude early on
  const ys = pts.map((p) => Math.log10(Math.max(p[2], 1e-3)));
  const lo = Math.min(...ys, target ? Math.log10(target) : Infinity), hi = Math.max(...ys);
  const x0 = pts[0][1], x1 = Math.max(pts[pts.length - 1][1], x0 + 1);
  const X = (it) => SPARK_PAD + ((SPARK_W - 2 * SPARK_PAD) * (it - x0)) / (x1 - x0);
  const Y = (v) => SPARK_PAD + ((SPARK_H - 2 * SPARK_PAD) * (hi - v)) / Math.max(hi - lo, 1e-6);
  if (target) {
    const ty = Y(Math.log10(target));
    mk("line", { x1: 0, y1: ty, x2: SPARK_W, y2: ty, class: "target" });
  }
  mk("polyline", { points: pts.map((p, i) => `${X(p[1]).toFixed(1)},${Y(ys[i]).toFixed(1)}`).join(" ") });
  const last = pts[pts.length - 1];
  mk("circle", { cx: X(last[1]), cy: Y(ys[ys.length - 1]), r: 2.5 });
  return svg;
}

const STATUS_TEXT = { queued: "Queued", running: "Solving", done: "Solved", error: "Failed", cancelled: "Cancelled" };

/**
 * Live progress panel: {el, update(job), setCancel(fn | null)}. `target` = preset accuracy
 * (% pot) for the sparkline's goal line; `maxIterations` shows "n / max".
 */
export function progressPanel({ title = "Solving", target = null, maxIterations = null } = {}) {
  const status = h("span", { class: "job-chip", role: "status" });
  const iters = h("b", { class: "big" }, "0");
  const expl = h("b", { class: "big" }, "–");
  const elapsed = h("b", { class: "big" }, "0 s");
  const sparkBox = h("div", { class: "job-spark-box" });
  let onCancel = null;
  const cancelBtn = h("button", { type: "button", class: "btn ghost job-cancel", hidden: true,
    onclick: async () => {
      if (!onCancel) return;
      cancelBtn.disabled = true;
      try { await onCancel(); } catch { cancelBtn.disabled = false; }
    } }, "Cancel");
  const el = h("section", { class: "job-panel", "aria-label": title, "aria-live": "polite" },
    h("div", { class: "job-head" }, h("span", { class: "label" }, title), status),
    h("div", { class: "job-stats" },
      h("div", {}, h("span", { class: "label" }, "Iterations"), iters),
      h("div", {}, h("span", { class: "label" }, "Exploitability"), expl),
      h("div", {}, h("span", { class: "label" }, "Elapsed"), elapsed)),
    sparkBox,
    h("div", { class: "job-foot" },
      h("span", { class: "muted mono" }, target ? `target ${num(target, 2)}% pot` : ""), cancelBtn));
  function update(j) {
    const st = j.status || "queued";
    el.dataset.status = st;
    status.textContent = STATUS_TEXT[st] || st;
    iters.textContent = j.iterations === null || j.iterations === undefined ? "0" : String(j.iterations);
    if (maxIterations) iters.append(h("small", {}, ` / ${maxIterations}`));
    expl.textContent = j.exploitability_pct_pot === null || j.exploitability_pct_pot === undefined ? "–" : `${num(j.exploitability_pct_pot, 2)}%`;
    if (j.exploitability_pct_pot !== null && j.exploitability_pct_pot !== undefined) expl.append(h("small", {}, " pot"));
    elapsed.textContent = `${Math.floor(j.elapsed_sec || 0)} s`;
    sparkBox.replaceChildren(sparkline(j.curve, target));
    cancelBtn.hidden = !onCancel || !(st === "queued" || st === "running");
  }
  function setCancel(fn) { onCancel = fn; cancelBtn.disabled = false; }
  update({ status: "queued" });
  return { el, update, setCancel };
}

/** Run a job into a progress panel with a working Cancel button; resolves with the solve name. */
export function runWithPanel(panel, path, body, alive) {
  let jobId = null;
  panel.setCancel(async () => { if (jobId) panel.update(await cancelJob(jobId)); });
  return runJob(path, body, { onUpdate: panel.update, onStart: (id) => { jobId = id; }, alive });
}
