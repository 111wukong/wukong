/* Boot and wiring.
 *
 * Everything here is glue: read the environment, wire the modules to each
 * other, and own the two loops (start a run, attach to one). The rendering
 * lives in `transcript.js`, the sidebar in `sidebar.js`, and so on -- this file
 * is where they are allowed to know about each other, which is what keeps the
 * import graph a tree instead of a cycle.
 */

import { TOKEN, consumeStream, json, post } from "./js/api.js";
import { $, el, emit, on, setText, state } from "./js/store.js";
import * as sidebar from "./js/sidebar.js";
import * as transcript from "./js/transcript.js";
import * as review from "./js/review.js";
import * as preview from "./js/preview.js";
import * as plan from "./js/plan.js";
import * as rewind from "./js/rewind.js";
import * as palette from "./js/palette.js";
import { money, tokens } from "./js/format.js";

/* Effects the UI may grant. SYSTEM_ADMIN is absent on purpose: the server
 * refuses it too, and offering a control that always fails is worse than not
 * offering it. */
const GRANTABLE = [
  "read_only",
  "write_local",
  "execute_local",
  "network",
  "external_side_effect",
];

const THREAD_KEY = "wukong.thread";
const DENSITY_KEY = "wukong.density";
const PLAN_KEY = "wukong.plan";

/* --------------------------------------------------------------- composer */

let composing = false;
let currentRun = null;
let pendingInterrupt = null;
let lastUsage = { tokens: 0, cost: 0 };
let runStartedAt = 0;
let statusTimer = null;

function autosize() {
  const area = $("goal");
  area.style.height = "auto";
  area.style.height = `${Math.min(220, area.scrollHeight)}px`;
}

function setSendMode(running) {
  const button = $("btn-send");
  if (!button) return;
  // There is no queue behind the button, so it must not imply one: a second
  // task while one is running would be a lie about what the runtime does.
  button.disabled = running;
  $("btn-cancel").disabled = !running;
}

/* -------------------------------------------------------------- the loop */

async function send(goal) {
  if (state.running) return;
  state.running = true;
  hideApproval();
  review.closeReview();
  $("btn-cancel").disabled = false;
  $("empty")?.classList.add("hidden");

  transcript.addTurn("user", "你").append(el("div", "body", goal));
  currentRun = transcript.newRun();
  state.run = currentRun;
  runStartedAt = Date.now();
  lastUsage = { tokens: 0, cost: 0 };

  try {
    const response = await fetch("/agui", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(TOKEN ? { "X-WUKONG-Token": TOKEN } : {}) },
      body: JSON.stringify({
        threadId: state.threadId,
        messages: [{ role: "user", content: goal }],
        model: currentModel() || undefined,
        approve: approvedEffects(),
        plan_approval: planWanted(),
      }),
    });
    if (!response.ok) throw new Error(`${response.status}: ${(await response.text()).slice(0, 300)}`);
    await consumeStream(response, transcript.renderEvent);
  } catch (error) {
    transcript.addTurn("error", "错误").append(el("div", "body", String(error)));
    transcript.setStatus("failed", false);
  } finally {
    if (!currentRun?.interrupted) {
      state.running = false;
      $("btn-cancel").disabled = true;
    }
    sidebar.refresh().catch(() => {});
    updateStatusline();
  }
}

/* Attach to a task, and re-attach if the connection drops while it is still
 * going.
 *
 * The server holds the stream open for the whole run and only closes it when
 * the task reaches a terminal state, so under normal conditions this loop runs
 * once. What it is for is the abnormal case: the server restarts, the machine
 * sleeps, the connection is reset. Without it the transcript simply stops
 * updating, with no error and no indication that anything is wrong -- the page
 * looks like the task is still thinking.
 */
async function attach(taskId) {
  const live = new Set(["pending", "planning", "running"]);
  for (;;) {
    if (state.taskId !== taskId) return; // the user moved to another task

    let response;
    try {
      response = await fetch(`/api/v1/tasks/${taskId}/stream`, {
        headers: TOKEN ? { "X-WUKONG-Token": TOKEN } : {},
      });
    } catch (error) {
      if (state.taskId !== taskId) return;
      console.warn("stream connect failed, retrying", error);
      await new Promise((resolve) => setTimeout(resolve, 1500));
      continue;
    }
    if (!response.ok) throw new Error(`${response.status} attaching to ${taskId}`);

    await consumeStream(response, transcript.renderEvent);
    if (state.taskId !== taskId) return;

    const task = await json(`/api/v1/tasks/${taskId}`).catch(() => null);
    if (!task || !live.has(task.status)) {
      state.running = false;
      updateStatusline();
      return;
    }
    console.warn(`stream ended while ${taskId} is ${task.status}; re-attaching`);
    await new Promise((resolve) => setTimeout(resolve, 800));
  }
}

/** Open a task: read-only replay, or a live stream if it is still moving. */
async function openTask(taskId) {
  if (!taskId) return;
  state.taskId = taskId;
  state.running = false;
  pendingInterrupt = null;
  hideApproval();
  review.closeReview();
  plan.clear();
  transcript.clearTranscript();
  sidebar.markActive(taskId);

  let task;
  try {
    task = await json(`/api/v1/tasks/${taskId}`);
  } catch (error) {
    transcript.addTurn("error", "错误").append(el("div", "body", `读不到任务：${error}`));
    return;
  }

  state.sessionId = task.session_id;
  setText($("topbar-title"), task.id);
  setText($("topbar-sub"), task.goal || "");
  if (task.goal) transcript.addTurn("user", "你").append(el("div", "body", task.goal));

  currentRun = transcript.newRun();
  state.run = currentRun;
  runStartedAt = task.created_at ? new Date(task.created_at).getTime() : Date.now();

  transcript.setStatus(task.orphaned ? "orphaned" : task.status, task.status === "running");
  $("btn-cancel").disabled = !["running", "pending", "planning", "waiting_confirmation"].includes(task.status);

  if (task.pending_confirmation) {
    pendingInterrupt = {
      id: task.pending_confirmation.request_id,
      tool: task.pending_confirmation.tool,
      effect: task.pending_confirmation.effect,
      preview: task.pending_confirmation.preview,
      detail: task.pending_confirmation.reason,
      review: task.pending_confirmation.review,
    };
    handleApproval(pendingInterrupt);
  }

  attach(taskId).catch((error) => console.warn("attach failed", error));
  updateStatusline(task);
}

async function cancel() {
  if (!state.taskId) return;
  // Read the answer. A paused task is cancelled outright and a running one is
  // only *asked* to stop, so "cancelling…" is wrong half the time -- and a
  // status line that lies is worse than no status line.
  try {
    const response = await fetch(`/api/v1/tasks/${state.taskId}/cancel`, {
      method: "POST",
      headers: TOKEN ? { "X-WUKONG-Token": TOKEN } : {},
    });
    const result = await response.json();
    if (result.outcome === "cancelled" || result.outcome === "already_terminal") {
      state.running = false;
      currentRun = transcript.newRun();
      state.run = currentRun;
      hideApproval();
      review.closeReview();
      $("btn-cancel").disabled = true;
      transcript.setStatus(result.status || "cancelled", false);
    } else {
      transcript.setStatus("cancelling", true);
    }
  } catch {
    transcript.setStatus("failed", false);
  }
}

/* ------------------------------------------------------------- approvals */

function handleApproval(interrupt) {
  if (!interrupt) return;
  pendingInterrupt = interrupt;

  if (interrupt.kind === "plan") {
    hideApproval();
    review.closeReview();
    plan.renderPlanApproval(interrupt);
    transcript.setStatus("waiting_confirmation", false);
    $("empty")?.classList.add("hidden");
    palette.setContext({ pending: true, reviewable: false });
    return;
  }

  const reviewable = Boolean(interrupt.review?.files?.length);
  palette.setContext({ pending: true, reviewable });

  if (reviewable) {
    hideApproval();
    review.openReview(state.taskId, interrupt).catch((error) => console.warn(error));
    $("empty")?.classList.add("hidden");
    transcript.setStatus("waiting_confirmation", false);
    return;
  }
  showApproval(interrupt);
}

function showApproval(interrupt) {
  $("approval-effect").textContent = interrupt.effect || "";
  renderPreview($("approval-preview"), interrupt.preview || "");
  $("approval-detail").textContent = interrupt.detail || "";
  $("approval").hidden = false;
  $("approval").dataset.requestId = interrupt.id || "";
  $("approval-preview").classList.remove("expanded");
  $("approval-expand").textContent = "展开";
  transcript.setStatus("waiting_confirmation", false);
  $("btn-cancel").disabled = false;
  $("empty")?.classList.add("hidden");
}

function hideApproval() {
  $("approval").hidden = true;
}

/* Colour a unified diff so the eye lands on the changed lines.
 *
 * The +/- prefixes are the only thing separating a diff from prose, and
 * uncoloured on a dark background they are nearly invisible -- which loses the
 * entire point of showing a diff instead of the payload. */
function renderPreview(node, text) {
  node.replaceChildren();
  const lines = String(text).split("\n");
  const looksLikeDiff =
    lines.some((line) => line.startsWith("+++") || line.startsWith("---")) &&
    lines.some((line) => /^[+-][^+-]/.test(line));
  if (!looksLikeDiff) {
    node.textContent = text;
    return;
  }
  for (const line of lines) {
    let cls = "diff-line";
    if (/^(\+\+\+|---)/.test(line)) cls += " diff-meta";
    else if (line.startsWith("@@")) cls += " diff-hunk";
    else if (line.startsWith("+")) cls += " diff-add";
    else if (line.startsWith("-")) cls += " diff-del";
    node.append(el("div", cls, line));
  }
}

async function decide(verb) {
  if (!state.taskId) {
    transcript.setStatus("failed", false);
    transcript.addTurn("error", "错误").append(
      el("div", "body", "不知道该批准哪个任务 —— 界面上没有选中的任务。"),
    );
    return;
  }
  hideApproval();
  review.closeReview();
  pendingInterrupt = null;
  palette.setContext({ pending: false, reviewable: false });
  transcript.setStatus(verb === "approve" ? "running" : "idle", true);
  try {
    await post(`/api/v1/tasks/${state.taskId}/${verb}`, {});
    // Re-open rather than append: attaching replays the run from its first
    // event, so appending to what is already on screen would show every step
    // twice. Re-opening is also what makes a reload and an approval produce
    // the same transcript, which is the only way the two can be trusted to
    // agree.
    await openTask(state.taskId);
  } catch (error) {
    // A 409 here means the request went to a task that was not waiting, which
    // is a stale id rather than anything the user did wrong. Naming the task
    // makes it diagnosable instead of just "409".
    transcript.addTurn("error", "错误").append(
      el(
        "div",
        "body",
        `${verb === "approve" ? "批准" : "拒绝"}失败：${error}\n打到的任务是 ${state.taskId}`,
      ),
    );
    transcript.setStatus("failed", false);
  }
}

/* ---------------------------------------------------------------- status */

function updateStatusline(task = null) {
  const bar = $("statusline");
  if (!bar) return;
  const parts = [];
  const model = currentModel();
  if (model) parts.push({ text: `模型 ${model}` });
  if (state.taskId) parts.push({ text: `任务 ${state.taskId.slice(-6)}` });
  if (task) {
    parts.push({ text: `${task.steps || 0} 步` });
    parts.push({ text: `${tokens(task.tokens || 0)} token` });
    if (task.cost_usd) parts.push({ text: money(task.cost_usd) });
    // How full the window is, as a percentage. "How much have I spent" is a
    // different number, and it is the one that cannot tell you whether the
    // next step will fit.
    if (task.context?.limit) {
      const ratio = Number(task.context.ratio) || 0;
      // "<1%" rather than "0%": a window that is a fifth of a percent full
      // and one that is empty are the same number at zero decimal places, and
      // "0%" reads as "not measured".
      const pct = ratio > 0 && ratio < 0.01 ? "<1%" : `${Math.round(ratio * 100)}%`;
      parts.push({
        text: `上下文 ${pct}`,
        tone: ratio >= 0.9 ? "danger" : ratio >= 0.7 ? "warn" : "",
        title: `${task.context.used} / ${task.context.limit} token`,
      });
    }
  } else if (lastUsage.tokens) {
    parts.push({ text: `${tokens(lastUsage.tokens)} token` });
  }

  bar.replaceChildren();
  parts.forEach((part, index) => {
    if (index) bar.append(el("span", "sep", "·"));
    const node = el("span", part.tone || "", part.text);
    if (part.title) node.title = part.title;
    bar.append(node);
  });
  bar.append(el("span", "spacer"));
  if (state.taskId) bar.append(el("span", "", state.threadId.slice(0, 18)));
}

async function pollStatus() {
  try {
    const health = await json("/api/v1/health");
    state.runningTasks = health.running_tasks || 0;
    setConn(health.running_tasks > 0 ? "busy" : "ok");
    if (state.taskId) {
      const task = await json(`/api/v1/tasks/${state.taskId}`);
      palette.setContext({ hasTask: true, hasTranscript: true });
      renderBudget(task);
      updateStatusline(task);
      if (task.pending_confirmation && !pendingInterrupt) {
        handleApproval({
          id: task.pending_confirmation.request_id,
          tool: task.pending_confirmation.tool,
          effect: task.pending_confirmation.effect,
          preview: task.pending_confirmation.preview,
          detail: task.pending_confirmation.reason,
          review: task.pending_confirmation.review,
        });
      }
    }
  } catch {
    setConn("down");
  }
}

function setConn(stateName) {
  const node = $("conn");
  if (!node) return;
  node.dataset.state = stateName === "ok" ? "" : stateName;
  setText(node.querySelector("span"), stateName === "down" ? "离线" : stateName === "busy" ? "运行中" : "本地");
}

/* How much of the budget is gone.
 *
 * A run that ends on `steps budget exhausted` should not come as a surprise:
 * the limit was known from the first step, and watching it climb is the
 * difference between "it stopped" and "it ran out". */
const budgetCache = { taskId: null, limits: {} };

async function renderBudget(task) {
  const meta = $("topbar-meta");
  if (!task) {
    meta.replaceChildren();
    return;
  }
  if (budgetCache.taskId !== task.id) {
    budgetCache.taskId = task.id;
    budgetCache.limits = {};
    try {
      budgetCache.limits = (await json(`/api/v1/tasks/${task.id}`)).budgets || {};
    } catch {
      /* the bar is a nicety; the run does not depend on it */
    }
  }

  const steps = Number(task.steps || 0);
  const total = Number(task.tokens || 0);
  const cost = Number(task.cost_usd || 0);
  const limit = Number(budgetCache.limits.max_steps || 0);
  const wanted = limit
    ? `${steps}/${limit} 步 · ${tokens(total)} · ${money(cost)}`
    : `${steps} 步 · ${tokens(total)} · ${money(cost)}`;

  if (meta.dataset.wanted === wanted) return;
  meta.dataset.wanted = wanted;
  meta.replaceChildren(el("span", "budget-text", wanted));
  if (!limit) return;
  const ratio = Math.min(1, steps / limit);
  const tone = ratio >= 0.8 ? "danger" : ratio >= 0.5 ? "warn" : "ok";
  const bar = el("span", `budget-bar ${tone}`);
  const fill = el("span", "budget-fill");
  fill.style.width = `${Math.round(ratio * 100)}%`;
  bar.append(fill);
  meta.append(bar);
}

/* ------------------------------------------------------------ permissions */

function buildApprovalToggles() {
  const box = $("approve");
  box.replaceChildren();
  for (const effect of GRANTABLE) {
    const button = el("button", "toggle", effect);
    button.type = "button";
    // aria-pressed, not a class: the state has to be visible to a screen reader
    // and to the CSS, and a native multi-select could not say it at all.
    button.setAttribute("aria-pressed", "false");
    button.onclick = () => {
      const on = button.getAttribute("aria-pressed") === "true";
      button.setAttribute("aria-pressed", on ? "false" : "true");
      updatePermChip();
    };
    box.append(button);
  }
}

/* Whether this run should pause after planning. Read at send time rather than
 * cached in state, so the switch and the request cannot disagree. */
function planWanted() {
  return $("chip-plan")?.getAttribute("aria-pressed") === "true";
}

function approvedEffects() {
  return [...$("approve").querySelectorAll('.toggle[aria-pressed="true"]')].map(
    (button) => button.textContent,
  );
}

function updatePermChip() {
  const count = approvedEffects().length;
  setText($("chip-perms-text"), count ? `已授权 ${count} 档` : "默认权限");
}

/* ----------------------------------------------------------------- models */

function currentModel() {
  return localStorage.getItem("wukong.model") || state.defaultModel;
}

function setModel(alias) {
  localStorage.setItem("wukong.model", alias);
  setText($("chip-model-text"), alias);
  updateStatusline();
}

async function loadModels() {
  try {
    const payload = await json("/api/v1/models");
    state.models = payload.models || [];
    state.defaultModel = payload.default || "mock";
  } catch {
    state.models = [];
    state.defaultModel = "mock";
  }
  setModel(currentModel());
  const list = $("model-list");
  list.replaceChildren();
  for (const model of state.models) {
    const row = el("button", "model-option");
    row.type = "button";
    row.append(el("span", "alias", model.alias));
    if (model.model) row.append(el("span", "provider", model.model));
    row.setAttribute("aria-selected", model.alias === currentModel() ? "true" : "false");
    row.onclick = () => {
      setModel(model.alias);
      closePops();
    };
    list.append(row);
  }
}

/* ---------------------------------------------------------------- popovers */

function closePops() {
  $("perms-pop").hidden = true;
  $("model-pop").hidden = true;
  $("chip-perms").setAttribute("aria-expanded", "false");
  $("chip-model").setAttribute("aria-expanded", "false");
}

function togglePop(pop, chip) {
  const willOpen = pop.hidden;
  closePops();
  if (!willOpen) return;
  pop.hidden = false;
  chip.setAttribute("aria-expanded", "true");
  const chipBox = chip.getBoundingClientRect();
  const appBox = $("main").getBoundingClientRect();
  pop.style.left = `${Math.max(12, chipBox.left - appBox.left)}px`;
  pop.style.bottom = `${appBox.bottom - chipBox.top + 8}px`;
}

/* ---------------------------------------------------------------- skills */

const PENDING_STATUS = new Set(["candidate", "validated", "approved"]);
const NEXT_STATUS = { candidate: "validated", validated: "approved", approved: "active" };
const SKILL_TONE = {
  active: "chip-ok",
  approved: "chip-accent",
  validated: "chip-warn",
  candidate: "chip-warn",
  deprecated: "",
};

async function loadSkills() {
  const list = $("skills");
  let skills;
  try {
    skills = await json("/api/v1/skills");
  } catch (error) {
    list.replaceChildren(el("div", "skill-error", `读取失败：${error.message || error}`));
    return;
  }

  setText($("skill-count"), skills.length ? String(skills.length) : "");
  // A closed drawer hides the one thing that needs a decision. It opens for
  // anything waiting, and only then -- a drawer that opens itself for no reason
  // is noise.
  if (skills.some((skill) => PENDING_STATUS.has(skill.status))) $("skills-fold").open = true;

  list.replaceChildren();
  if (!skills.length) {
    list.append(el("div", "skill-empty", "还没有技能。"));
    return;
  }
  for (const skill of skills) list.append(skillRow(skill));
}

function skillRow(skill) {
  const row = el("div", `skill-row status-${skill.status}`);
  const head = el("div", "skill-head");
  head.append(el("span", "skill-name", skill.name));
  head.append(el("span", `chip ${SKILL_TONE[skill.status] || ""}`.trim(), skill.status));
  row.append(head);
  if (skill.description) row.append(el("div", "skill-desc", skill.description));

  const next = NEXT_STATUS[skill.status];
  if (!next) return row;

  const actions = el("div", "skill-actions");
  const button = el("button", "btn btn-ghost skill-promote", `晋级为 ${next}`);
  button.type = "button";
  button.onclick = async (event) => {
    event.stopPropagation();
    button.disabled = true;
    setText(button, "处理中…");
    try {
      await post(`/api/v1/skills/${encodeURIComponent(skill.name)}/promote`, { status: next });
    } catch (error) {
      setText(button, `失败：${error.message || error}`);
      button.disabled = false;
      return;
    }
    loadSkills();
  };
  actions.append(button);
  row.append(actions);
  return row;
}

/* ---------------------------------------------------------------- sandbox */

async function loadSandbox() {
  const box = $("sandbox");
  let payload;
  try {
    payload = await json("/api/v1/sandbox");
  } catch (error) {
    box.replaceChildren(el("div", "sandbox-note", `读取失败：${error.message || error}`));
    return;
  }
  box.replaceChildren();
  box.append(sandboxRow("后端", payload.backend || "unknown", payload.backend === "none" ? "warn" : "ok"));
  box.append(sandboxRow("隔离", payload.isolation || "unknown", payload.isolation === "none" ? "warn" : "ok"));
  if (payload.fell_back) box.append(sandboxRow("回退", "已回退到无沙箱", "danger"));
  for (const note of payload.notes || []) {
    box.append(el("div", "sandbox-note", note));
  }
  for (const caveat of payload.caveats || []) {
    box.append(el("div", "sandbox-note", caveat));
  }
}

function sandboxRow(key, value, tone) {
  const row = el("div", "sandbox-row");
  row.append(el("span", "sandbox-key", key));
  row.append(el("span", `sandbox-val ${tone || ""}`.trim(), value));
  return row;
}

/* ------------------------------------------------------------------ toasts */

function toast({ text, tone }) {
  const node = el("div", `toast ${tone || ""}`.trim(), text);
  $("toasts").append(node);
  setTimeout(() => node.remove(), 3200);
}

/* ------------------------------------------------------------------ export */

function exportMarkdown() {
  const lines = [];
  lines.push(`# wukong 会话 ${state.threadId}`);
  lines.push("");
  lines.push(`任务：${state.taskId || "（未开始）"}`);
  lines.push(`导出时间：${new Date().toLocaleString("zh-CN")}`);
  lines.push("");

  for (const turn of $("transcript").querySelectorAll(".turn")) {
    if (turn.id === "empty") continue;
    if (turn.classList.contains("user")) {
      lines.push("## 你", "", turn.querySelector(".body")?.textContent || "", "");
      continue;
    }
    if (turn.classList.contains("error")) {
      lines.push("## 错误", "", turn.querySelector(".body")?.textContent || "", "");
      continue;
    }
    lines.push("## Agent", "");
    for (const child of turn.children) {
      if (child.classList.contains("turn-head")) continue;
      if (child.classList.contains("body")) {
        lines.push(child.textContent || "");
        continue;
      }
      if (child.classList.contains("tool")) {
        const name = child.querySelector(".tool-name")?.textContent || "tool";
        const arg = child.querySelector(".tool-arg")?.textContent || "";
        const mark = child.querySelector(".tool-mark")?.textContent || "";
        lines.push(`- ${mark} \`${name}\` ${arg}`.trim());
        const result = child.querySelector(".tool-result");
        if (result) {
          const text = result.textContent || "";
          lines.push("", "```", text.slice(0, 4000), "```", "");
        }
        continue;
      }
      if (child.classList.contains("plan-card")) {
        lines.push("", "**计划**", "");
        for (const step of child.querySelectorAll(".plan-step")) {
          lines.push(`- ${step.querySelector(".mark")?.textContent || ""} ${step.querySelector(".step-text")?.textContent || ""}`);
        }
        lines.push("");
      }
    }
    lines.push("");
  }

  const blob = new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = el("a", "");
  anchor.href = url;
  anchor.download = `wukong-${state.taskId || "session"}.md`;
  anchor.click();
  URL.revokeObjectURL(url);
  toast({ text: "已导出 Markdown", tone: "ok" });
}

/* ------------------------------------------------------------- commands */

const ACTIONS = {
  "new-task": () => newTask(),
  focus: () => $("goal").focus(),
  approve: () => decide("approve"),
  deny: () => decide("deny"),
  review: () => {
    if (pendingInterrupt) review.openReview(state.taskId, pendingInterrupt);
  },
  cancel: () => cancel(),
  resume: () => {
    if (!state.taskId) return;
    post(`/api/v1/tasks/${state.taskId}/resume`, {})
      .then(() => openTask(state.taskId))
      .catch((error) => toast({ text: `继续失败：${error.message || error}`, tone: "err" }));
  },
  rewind: () => rewind.openRewind(state.taskId),
  export: () => exportMarkdown(),
  "copy-last": () => {
    const bodies = $("transcript").querySelectorAll(".turn.assistant .body");
    const text = bodies.length ? bodies[bodies.length - 1].textContent || "" : "";
    navigator.clipboard?.writeText(text).then(
      () => toast({ text: "已复制最后一条回复", tone: "ok" }),
      () => toast({ text: "复制失败", tone: "err" }),
    );
  },
  density: () => toggleDensity(),
  refresh: () => {
    sidebar.refresh().then(() => toast({ text: "已刷新", tone: "ok" }));
  },
  help: () => palette.openHelp(),
  escape: () => {
    if (review.isOpen()) review.closeReview();
    else if (!$("preview").hidden) preview.closePreview();
    else if (!$("approval").hidden) hideApproval();
    else if (state.running) cancel();
  },
};

function newTask() {
  state.threadId = `console-${Math.random().toString(36).slice(2, 8)}`;
  localStorage.setItem(THREAD_KEY, state.threadId);
  state.taskId = null;
  state.sessionId = null;
  state.running = false;
  setText($("topbar-title"), "新任务");
  setText($("topbar-sub"), "");
  pendingInterrupt = null;
  hideApproval();
  review.closeReview();
  plan.clear();
  preview.closePreview();
  transcript.clearTranscript();
  sidebar.markActive(null);
  sidebar.setFilter($("search").value);
  $("goal").value = "";
  autosize();
  $("goal").focus();
  updateStatusline();
}

function toggleDensity() {
  const root = document.documentElement;
  const next = root.dataset.density === "compact" ? "comfortable" : "compact";
  root.dataset.density = next;
  localStorage.setItem(DENSITY_KEY, next);
  toast({ text: next === "compact" ? "紧凑密度" : "舒适密度" });
}

/* Slash commands in the composer.
 *
 * The composer is where a user already is, so `/` is worth more than a menu:
 * the common actions cost four keystrokes and no mouse, and the palette stays
 * for the ones you cannot remember.
 */
function runSlash(text) {
  const [head, ...rest] = text.trim().split(/\s+/);
  const arg = rest.join(" ");
  switch (head) {
    case "/new":
      newTask();
      return true;
    case "/clear":
      transcript.clearTranscript();
      return true;
    case "/export":
      exportMarkdown();
      return true;
    case "/help":
      palette.openHelp();
      return true;
    case "/density":
      toggleDensity();
      return true;
    case "/approve":
      decide("approve");
      return true;
    case "/deny":
      decide("deny");
      return true;
    case "/model":
      if (arg) {
        setModel(arg);
        toast({ text: `模型切到 ${arg}`, tone: "ok" });
      } else {
        togglePop($("model-pop"), $("chip-model"));
      }
      return true;
    default:
      return false;
  }
}

/* -------------------------------------------------------------------- boot */

function bindComposer() {
  const area = $("goal");

  area.addEventListener("input", autosize);
  // With a Chinese IME, Enter picks a pinyin candidate. Without this check that
  // Enter also sends, and the agent starts on a half-typed sentence.
  area.addEventListener("compositionstart", () => {
    composing = true;
  });
  area.addEventListener("compositionend", () => {
    composing = false;
  });

  area.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    if (event.isComposing || composing) return;
    if (event.metaKey || event.ctrlKey) {
      event.preventDefault();
      submit();
      return;
    }
    if (event.shiftKey) return; // newline
    event.preventDefault();
    submit();
  });

  $("composer").addEventListener("submit", (event) => {
    event.preventDefault();
    submit();
  });
}

function submit() {
  const area = $("goal");
  const goal = area.value.trim();
  if (!goal || state.running) return;

  if (goal.startsWith("/")) {
    if (runSlash(goal)) {
      area.value = "";
      autosize();
      return;
    }
  }

  area.value = "";
  autosize();
  send(goal);
}

function bindEvents() {
  $("btn-new").onclick = () => newTask();
  $("btn-palette").onclick = () => palette.openPalette("");
  $("btn-export").onclick = () => exportMarkdown();
  $("btn-cancel").onclick = () => cancel();
  $("btn-send").onclick = (event) => {
    event.preventDefault();
    submit();
  };
  $("btn-approve").onclick = () => decide("approve");
  $("btn-deny").onclick = () => decide("deny");
  $("approval-expand").onclick = () => {
    const pre = $("approval-preview");
    const expanded = pre.classList.toggle("expanded");
    $("approval-expand").textContent = expanded ? "收起" : "展开";
  };

  $("chip-plan").onclick = () => {
    const on = $("chip-plan").getAttribute("aria-pressed") === "true";
    $("chip-plan").setAttribute("aria-pressed", on ? "false" : "true");
    localStorage.setItem(PLAN_KEY, on ? "0" : "1");
  };
  $("chip-perms").onclick = (event) => {
    event.stopPropagation();
    togglePop($("perms-pop"), $("chip-perms"));
  };
  $("chip-model").onclick = (event) => {
    event.stopPropagation();
    togglePop($("model-pop"), $("chip-model"));
  };
  $("model-input").addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    const value = $("model-input").value.trim();
    if (!value) return;
    setModel(value);
    $("model-input").value = "";
    closePops();
  });
  document.addEventListener("click", (event) => {
    if (event.target.closest(".pop") || event.target.closest(".chip-btn")) return;
    closePops();
  });

  $("review-close").onclick = () => review.closeReview();
  $("review-accept").onclick = () => review.acceptReview();
  $("review-deny").onclick = () => review.denyReview();
  $("review-all").onchange = (event) => review.selectAll(event.target.checked);

  $("preview-close").onclick = () => preview.closePreview();
  $("preview-edit").onclick = () => preview.startEditing();

  $("help-close").onclick = () => palette.closeHelp();
  $("help").onclick = (event) => {
    if (event.target === $("help")) palette.closeHelp();
  };
  $("palette").onclick = (event) => {
    if (event.target === $("palette")) palette.closePalette();
  };

  $("search").addEventListener("input", (event) => sidebar.setFilter(event.target.value));

  // The tree is read when it is first opened, not at boot: a workspace with a
  // `node_modules` in it makes the first paint cost a directory walk nobody
  // asked for yet.
  $("files-fold").addEventListener("toggle", () => {
    if ($("files-fold").open && !$("tree").dataset.loaded) {
      sidebar.loadTree().catch(() => {});
    }
  });

  for (const example of document.querySelectorAll(".example")) {
    example.onclick = () => {
      $("goal").value = example.dataset.goal || "";
      autosize();
      $("goal").focus();
    };
  }

  on("open-task", (taskId) => openTask(taskId));
  on("command", (id) => {
    const action = ACTIONS[id];
    if (action) action();
  });
  on("approval", (interrupt) => handleApproval(interrupt));
  on("task-id", (taskId) => {
    setText($("topbar-title"), taskId);
    sidebar.markActive(taskId);
    sidebar.refresh().catch(() => {});
  });
  on("open-file", (path) => {
    preview.openPreview(path).catch((error) => console.warn("preview failed", error));
  });
  on("resume", (taskId) => {
    transcript.setStatus("running", true);
    openTask(taskId).catch((error) => console.warn(error));
  });
  on("scroll", () => transcript.scrollToBottom());
  on("files-changed", () => {
    // The tree caches what it read; after a rewind that cache is wrong.
    if ($("files-fold").open) sidebar.loadTree().catch(() => {});
  });
  on("plan-decided", () => {
    pendingInterrupt = null;
    palette.setContext({ pending: false, reviewable: false });
  });
  on("review-decided", () => {
    pendingInterrupt = null;
    palette.setContext({ pending: false, reviewable: false });
  });
  on("error", (message) => toast({ text: message, tone: "err" }));
  on("toast", (payload) => toast(payload));
  on("usage", (value) => {
    lastUsage = { tokens: value.totalTokens || 0, cost: value.costUsd || 0 };
  });
  on("running", (running) => {
    setSendMode(running);
    // A run can finish while the tab is in the background. The title is the
    // only thing a background tab can say, and "it finished" is worth saying.
    if (running) {
      document.title = "wukong 控制台";
    } else if (document.hidden) {
      document.title = "● 已结束 · wukong";
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) document.title = "wukong 控制台";
  });
}

async function boot() {
  const savedDensity = localStorage.getItem(DENSITY_KEY);
  if (savedDensity) document.documentElement.dataset.density = savedDensity;

  if (localStorage.getItem(PLAN_KEY) === "1") {
    $("chip-plan").setAttribute("aria-pressed", "true");
  }

  const savedThread = localStorage.getItem(THREAD_KEY);
  if (savedThread) state.threadId = savedThread;
  else localStorage.setItem(THREAD_KEY, state.threadId);

  buildApprovalToggles();
  bindComposer();
  bindEvents();
  palette.bindKeys();
  setSendMode(false);
  autosize();

  try {
    const health = await json("/api/v1/health");
    setText($("version"), `v${health.version || ""}`);
    setConn("ok");
  } catch {
    setConn("down");
  }

  loadModels().catch(() => {});
  loadSandbox().catch(() => {});
  loadSkills().catch(() => {});
  await sidebar.refresh().catch(() => {});
  state.tasks = sidebar.allTasks();

  updateStatusline();
  statusTimer = setInterval(() => {
    pollStatus();
    state.tasks = sidebar.allTasks();
  }, 4000);

  // Attach to the newest task if the page was reloaded mid-run, so the tab does
  // not come back looking empty while something is still working.
  const newest = state.tasks[0];
  if (newest && ["running", "waiting_confirmation", "planning"].includes(newest.status)) {
    openTask(newest.id);
  } else if (newest) {
    openTask(newest.id);
  }
}

boot().catch((error) => {
  console.error(error);
  toast({ text: `启动失败：${error.message || error}`, tone: "err" });
});

