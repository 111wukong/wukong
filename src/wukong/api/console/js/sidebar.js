/* The sidebar: sessions, then the tasks inside the selected one.
 *
 * The console used to list tasks only. A reload left the previous run reachable
 * by id and by nothing else, so the honest description of that version is "you
 * may not close this tab". Sessions are the level at which someone actually
 * remembers work -- "the refactor", "the release notes" -- and tasks are the
 * attempts inside it.
 *
 * Both lists come from one request each and are joined here. Per-session
 * requests would be N+1 and would make the sidebar the slowest thing on the
 * page for no reason.
 */

import { json } from "./api.js";
import { $, el, emit, setClass, setText, state } from "./store.js";
import { bytes, dayBucket, money, timeAgo, tokens } from "./format.js";
import { subtreeTotals } from "./tree.js";

const STATUS_TONE = {
  completed: "chip-ok",
  failed: "chip-danger",
  cancelled: "chip-danger",
  waiting_confirmation: "chip-warn",
  running: "chip-accent",
  planning: "chip-accent",
  pending: "chip-accent",
  orphaned: "chip-warn",
};

// Short label, full value in the tooltip. `waiting_confirmation` is a wire
// value; as a UI label it is both long and jargon, and it wrapped the row.
const STATUS_LABEL = {
  pending: "排队中",
  planning: "规划中",
  running: "运行中",
  // Not a status the server stores -- it is computed per request from "the
  // record says running, and this process has never heard of it". It gets its
  // own word because "运行中" is what makes a user wait for something that is
  // never going to move.
  orphaned: "已中断",
  waiting: "等待批准",
  waiting_confirmation: "等待批准",
  cancelling: "正在取消",
  resyncing: "重新同步",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  idle: "空闲",
};

const TONE = STATUS_TONE;
const LABEL = STATUS_LABEL;

let sessions = [];
let tasks = [];
let filter = "";
const taskNodes = new Map();

export function statusLabel(key) {
  return LABEL[key] || key;
}

export function statusTone(key) {
  return TONE[key] || "";
}

export function allTasks() {
  return tasks;
}

/* Parents before their children, children indented.
 *
 * A fan-out run creates one task per sub-agent. Shown as peers, "fan out to
 * five" reads as six unrelated runs -- and the sub-agent that failed is
 * indistinguishable from the one that called it. The relationship was already
 * in the data (`parent_task_id`); nothing was reading it.
 */
function flatten(rows) {
  const byId = new Map(rows.map((task) => [task.id, task]));
  const kids = new Map();
  const roots = [];

  for (const task of rows) {
    const parent = task.parent_task_id;
    // A parent outside this page (older than the limit) is not a parent here.
    // Showing the child at the top level beats dropping it off the list.
    if (parent && byId.has(parent)) {
      if (!kids.has(parent)) kids.set(parent, []);
      kids.get(parent).push(task);
    } else {
      roots.push(task);
    }
  }

  const out = [];
  const walk = (task, depth) => {
    out.push({ task, depth });
    for (const child of kids.get(task.id) || []) walk(child, depth + 1);
  };
  for (const root of roots) walk(root, 0);
  return out;
}

export async function loadSidebar() {
  await Promise.all([loadSessions(), loadTasks()]);
}

async function loadSessions() {
  const list = $("sessions");
  try {
    sessions = await json("/api/v1/sessions?limit=40");
  } catch (error) {
    list.replaceChildren(el("div", "session-empty", `读取失败：${error.message || error}`));
    return;
  }
  renderSessions();
}

async function loadTasks() {
  const list = $("tasks");
  try {
    tasks = await json("/api/v1/tasks?limit=80");
  } catch (error) {
    list.replaceChildren(el("div", "task-empty", `读取失败：${error.message || error}`));
    return;
  }
  renderTasks();
}

function matches(text) {
  return !filter || String(text || "").toLowerCase().includes(filter);
}

function sessionTitle(session) {
  // The first task's goal is what the user remembers, not the session's uuid.
  const first = tasks
    .filter((task) => task.session_id === session.id)
    .sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)))[0];
  const goal = first?.goal || session.name || "会话";
  return goal.length > 40 ? `${goal.slice(0, 39)}…` : goal;
}

function renderSessions() {
  const list = $("sessions");
  const visible = sessions.filter((session) => {
    if (!filter) return true;
    if (matches(session.name)) return true;
    return tasks.some(
      (task) => task.session_id === session.id && matches(task.goal),
    );
  });

  setText($("session-count"), sessions.length ? String(sessions.length) : "");
  list.replaceChildren();

  if (!visible.length) {
    list.append(
      el("div", "session-empty", filter ? "没有匹配的会话。" : "还没有会话。"),
    );
    return;
  }

  let bucket = "";
  for (const session of visible) {
    const label = dayBucket(session.last_active_at || session.created_at);
    if (label !== bucket) {
      bucket = label;
      list.append(el("div", "session-group-label", label));
    }

    const row = el("div", "session-item");
    row.dataset.id = session.id;
    if (session.id === state.sessionId) row.classList.add("active");

    if (session.waiting_count) row.append(el("span", "dot waiting"));
    else if (session.running_count) row.append(el("span", "dot"));

    row.append(el("span", "session-name", sessionTitle(session)));
    row.append(
      el("span", "session-meta", timeAgo(session.last_active_at || session.created_at)),
    );
    row.title = `${session.id} · ${session.task_count || 0} 个任务`;
    row.onclick = () => {
      state.sessionId = session.id;
      renderSessions();
      renderTasks();
      const newest = tasks.find((task) => task.session_id === session.id);
      if (newest) emit("open-task", newest.id);
      emit("session", session.id);
    };
    list.append(row);
  }
}

function paintTask(node, task, subtree) {
  setText(node.goal, task.goal);
  node.goal.title = task.goal;
  // `orphaned` is computed by the server, not stored: the record says running
  // and this process has never heard of the task. Painting that as "运行中" is
  // a lie the user acts on by waiting for something that will never move.
  const shown = task.orphaned ? "orphaned" : task.status;
  setClass(node.chip, `chip ${TONE[shown] || ""}`.trim());
  setText(node.chip, LABEL[shown] || task.status);
  node.chip.title = task.orphaned
    ? "崩溃遗留：记录说在跑，但没有进程在跑它。用 task resume 可以接着跑。"
    : task.status;
  // A parent's own budget hides the workers it spawned. When the row has
  // children in the current view, show the subtree total instead -- the same
  // number `task tree` reports, so the sidebar and the CLI cannot disagree.
  const ownTokens = (task.tokens_in || 0) + (task.tokens_out || 0);
  if (subtree && subtree.tasks > 1) {
    setText(
      node.stats,
      `${subtree.tasks - 1} 子任务 · 合计 ${subtree.steps} 步 · ${tokens(subtree.tokens)}`,
    );
    node.stats.title =
      `子树合计：${subtree.tasks - 1} 个子任务 · ${subtree.steps} 步 · ` +
      `${subtree.tokens} tokens · ${money(subtree.cost)}`;
  } else {
    setText(node.stats, `${task.steps_used || 0} 步 · ${tokens(ownTokens)}`);
    node.stats.title = "";
  }
  // The status *key*, not its label: the colour is chosen from this and the
  // text is looked up separately, so a translated label cannot lose the colour.
  node.item.dataset.state = shown;
  node.item.classList.toggle("active", task.id === state.taskId);
}

function renderTasks() {
  const list = $("tasks");
  let rows = tasks;
  if (state.sessionId) rows = rows.filter((task) => task.session_id === state.sessionId);
  if (filter) {
    rows = rows.filter(
      (task) => matches(task.goal) || matches(task.id),
    );
  }

  setText($("task-count"), rows.length ? String(rows.length) : "");
  list.replaceChildren();

  if (!rows.length) {
    list.append(
      el("div", "task-empty", filter ? "没有匹配的任务。" : "还没有任务。"),
    );
    taskNodes.clear();
    return;
  }

  const ordered = flatten(rows);
  const totals = subtreeTotals(rows);
  const ids = ordered.map((entry) => entry.task.id).join();
  if (ids !== [...taskNodes.keys()].join()) {
    // The *set* changed, so rebuild. Doing this on every poll destroys the
    // element under the pointer every few seconds: hover flickers, and a click
    // lands on a node that is already gone.
    const next = new Map();
    list.replaceChildren();
    for (const { task, depth } of ordered) {
      const node = taskNodes.get(task.id) || taskRow(task, depth);
      paintTask(node, task, totals.get(task.id));
      list.append(node.item);
      next.set(task.id, node);
    }
    taskNodes.clear();
    for (const [id, node] of next) taskNodes.set(id, node);
  } else {
    for (const { task } of ordered) {
      const node = taskNodes.get(task.id);
      if (node) paintTask(node, task, totals.get(task.id));
    }
  }
}

function taskRow(task, depth = 0) {
  const item = el("div", `task-item depth-${Math.min(depth, 3)}`);
  const goal = el("div", "task-goal");
  if (depth > 0) goal.append(el("span", "task-child-mark", "↳ "));
  const meta = el("div", "task-meta");
  const chip = el("span", "chip");
  const stats = el("span", "task-stats");
  meta.append(chip, stats);
  item.append(goal, meta);
  item.onclick = () => {
    state.sessionId = task.session_id;
    emit("open-task", task.id);
    renderSessions();
  };
  return { item, goal, chip, stats };
}

export function setFilter(value) {
  filter = String(value || "").trim().toLowerCase();
  renderSessions();
  renderTasks();
}

/** Re-read both lists, keeping the scroll position the reader had. */
export async function refresh() {
  const list = $("tasks");
  const at = list ? list.scrollTop : 0;
  await loadSidebar();
  if (list) list.scrollTop = at;
}

export function markActive(taskId) {
  state.taskId = taskId;
  for (const [id, node] of taskNodes) node.item.classList.toggle("active", id === taskId);
  for (const row of $("sessions").querySelectorAll(".session-item")) {
    row.classList.toggle("active", row.dataset.id === state.sessionId);
  }
}

/* --------------------------------------------------------------- file tree
 *
 * Lazy on purpose: a directory is fetched when it is opened, not when it is
 * listed. The eager version walks the whole project on every page load, which
 * on a repository with a `node_modules` in it is a slow way to populate a
 * sidebar nobody asked to expand.
 */
export async function loadTree(path = "", container = $("tree"), depth = 0) {
  let payload;
  try {
    payload = await json(`/api/v1/files?path=${encodeURIComponent(path)}`);
  } catch (error) {
    container.append(el("div", "tree-empty", `读取失败：${error.message || error}`));
    return;
  }
  if (depth === 0) {
    container.replaceChildren();
    setText($("tree-count"), String(payload.entries.length));
  }
  container.dataset.loaded = path;

  if (!payload.entries.length) {
    container.append(el("div", "tree-empty", "（空目录）"));
    return;
  }

  for (const entry of payload.entries) {
    const isDir = entry.type === "dir";
    const row = el("div", `tree-row ${isDir ? "dir" : "file"}`);
    const label = el("button", "tree-name");
    label.type = "button";
    label.append(el("span", "tree-icon", isDir ? "▸" : "·"));
    label.append(el("span", "tree-text", entry.name));
    if (!isDir && entry.size !== null) {
      label.append(el("span", "tree-size", bytes(entry.size)));
    }
    const childPath = path ? `${path}/${entry.name}` : entry.name;
    row.append(label);
    container.append(row);

    if (!isDir) {
      label.addEventListener("click", () => emit("open-file", childPath));
      continue;
    }
    const kids = el("div", "tree-children");
    kids.hidden = true;
    container.append(kids);
    label.addEventListener("click", async () => {
      const opening = kids.hidden;
      kids.hidden = !opening;
      setText(label.querySelector(".tree-icon"), opening ? "▾" : "▸");
      if (opening && !kids.dataset.loaded) await loadTree(childPath, kids, depth + 1);
    });
  }
}
