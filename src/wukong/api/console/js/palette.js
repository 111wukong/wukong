/* Command palette, keymap, and the help sheet.
 *
 * This is the line between "a web page with buttons" and "a tool". A heavy user
 * presses keys hundreds of times a day; making them find a button each time is
 * a tax with no upside. The palette is also the discovery mechanism: everything
 * the console can do is in one list, searchable, instead of spread across
 * chrome that has to be visible at all times.
 *
 * Commands do not run here. They are emitted, and `app.js` dispatches -- which
 * keeps this file free of imports from the rest of the app and therefore free
 * of cycles.
 */

import { $, el, emit, state } from "./store.js";

const KEY = (() => {
  const mac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
  return { mod: mac ? "⌘" : "Ctrl", alt: mac ? "⌥" : "Alt" };
})();

export const KEYMAP = [
  { keys: ["mod+k"], id: "palette", label: "命令面板" },
  { keys: ["mod+n"], id: "new-task", label: "新任务" },
  { keys: ["?"], id: "help", label: "快捷键" },
  { keys: ["mod+shift+e"], id: "export", label: "导出为 Markdown" },
  { keys: ["mod+shift+d"], id: "density", label: "切换紧凑密度" },
  { keys: ["esc"], id: "escape", label: "关闭浮层 / 停止运行" },
  { keys: ["mod+enter"], id: "send", label: "发送（输入框内）" },
];

/* Every command the console has. `when` decides whether it is offered, so the
 * palette never shows an action that would immediately fail. */
const COMMANDS = [
  {
    id: "new-task",
    title: "新任务",
    hint: "开一个新的会话",
    icon: "＋",
    when: () => true,
  },
  {
    id: "focus",
    title: "回到输入框",
    hint: "开始描述任务",
    icon: "↵",
    when: () => true,
  },
  {
    id: "approve",
    title: "批准当前请求",
    hint: "整次调用",
    icon: "✓",
    when: (ctx) => ctx.pending,
  },
  {
    id: "deny",
    title: "拒绝当前请求",
    hint: "让 Agent 换个办法",
    icon: "✕",
    when: (ctx) => ctx.pending,
  },
  {
    id: "review",
    title: "打开改动审查",
    hint: "逐块接受",
    icon: "▤",
    when: (ctx) => ctx.pending && ctx.reviewable,
  },
  {
    id: "cancel",
    title: "停止当前运行",
    hint: "在下一步边界停下",
    icon: "■",
    when: (ctx) => ctx.running,
  },
  {
    id: "resume",
    title: "继续这个任务",
    hint: "从中断处接着跑",
    icon: "▷",
    when: (ctx) => ctx.resumable,
  },
  {
    id: "export",
    title: "导出为 Markdown",
    hint: "整段对话与工具结果",
    icon: "↓",
    when: (ctx) => ctx.hasTranscript,
  },
  {
    id: "copy-last",
    title: "复制最后一条回复",
    hint: "到剪贴板",
    icon: "⧉",
    when: (ctx) => ctx.hasTranscript,
  },
  {
    id: "density",
    title: "切换紧凑密度",
    hint: "一屏看更多步骤",
    icon: "≡",
    when: () => true,
  },
  {
    id: "refresh",
    title: "刷新会话与任务",
    hint: "重新读取侧栏",
    icon: "↻",
    when: () => true,
  },
  {
    id: "help",
    title: "查看快捷键",
    hint: "全部键位",
    icon: "?",
    when: () => true,
  },
];

let context = { pending: false, running: false, resumable: false, reviewable: false, hasTranscript: false };
let items = [];
let active = 0;
let query = "";

export function setContext(next) {
  context = { ...context, ...next };
}

export function openPalette(mode = "") {
  $("palette").hidden = false;
  const input = $("palette-input");
  input.value = mode;
  query = mode;
  active = 0;
  renderPalette();
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

export function closePalette() {
  $("palette").hidden = true;
  // Return focus to the page, not to a hidden input: `?` and `/` are ignored
  // while an input has focus, so leaving it there silently disables them.
  $("palette-input").blur();
}

export function paletteOpen() {
  return !$("palette").hidden;
}

export function helpOpen() {
  return !$("help").hidden;
}

export function openHelp() {
  const body = $("help-body");
  body.replaceChildren();
  for (const entry of KEYMAP) {
    const row = el("div", "help-row");
    row.append(el("span", "", entry.label));
    const keys = el("span", "keys");
    // One chip per token, which is how every keyboard-driven tool draws a
    // combination: `⌘` and `k` as separate caps read as "press both", where a
    // single cap containing "⌘ k" reads as a two-word label.
    for (const key of entry.keys) {
      for (const token of key.split("+")) keys.append(el("kbd", "", renderKey(token)));
    }
    row.append(keys);
    body.append(row);
  }
  $("help").hidden = false;
}

export function closeHelp() {
  $("help").hidden = true;
}

function renderKey(token) {
  return (
    {
      mod: KEY.mod,
      shift: "⇧",
      alt: KEY.alt,
      enter: "↵",
      esc: "esc",
    }[token] || token
  );
}

function visible() {
  return COMMANDS.filter((command) => command.when(context));
}

function renderPalette() {
  const list = $("palette-list");
  const raw = query.trim();
  const searchingTasks = raw.startsWith("@");
  const needle = (searchingTasks ? raw.slice(1) : raw.replace(/^[/>]/, "")).toLowerCase();

  list.replaceChildren();
  items = [];
  active = 0;

  if (searchingTasks) {
    const tasks = state.tasks || [];
    const found = tasks.filter((task) => String(task.goal || "").toLowerCase().includes(needle));
    list.append(el("div", "palette-group", "任务"));
    for (const task of found.slice(0, 20)) {
      items.push({ kind: "task", id: task.id, title: task.goal, hint: task.status });
      list.append(paletteRow(items.length - 1, "▷", task.goal, task.status));
    }
    if (!found.length) list.append(el("div", "palette-empty", "没有匹配的任务。"));
    highlight();
    return;
  }

  const found = visible().filter((command) => {
    if (!needle) return true;
    return (
      command.title.toLowerCase().includes(needle) ||
      command.id.includes(needle) ||
      String(command.hint || "").toLowerCase().includes(needle)
    );
  });

  list.append(el("div", "palette-group", "命令"));
  for (const command of found) {
    items.push({ kind: "command", id: command.id });
    list.append(paletteRow(items.length - 1, command.icon, command.title, command.hint));
  }
  if (!found.length) {
    list.append(el("div", "palette-empty", "没有匹配的命令。"));
  }
  highlight();
}

function paletteRow(index, icon, label, hint) {
  const row = el("button", "palette-item");
  row.type = "button";
  row.dataset.index = String(index);
  row.append(el("span", "p-icon", icon || "·"));
  row.append(el("span", "p-label", label));
  if (hint) row.append(el("span", "p-hint", hint));
  row.onclick = () => run(index);
  row.onmousemove = () => {
    if (active !== index) {
      active = index;
      highlight();
    }
  };
  return row;
}

function highlight() {
  const rows = $("palette-list").querySelectorAll(".palette-item");
  rows.forEach((row, index) => {
    row.setAttribute("aria-selected", index === active ? "true" : "false");
  });
  rows[active]?.scrollIntoView({ block: "nearest" });
}

function run(index) {
  const item = items[index];
  if (!item) return;
  closePalette();
  if (item.kind === "task") emit("open-task", item.id);
  else emit("command", item.id);
}

export function paletteKeydown(event) {
  if (!paletteOpen()) return false;
  if (event.key === "Escape") {
    event.preventDefault();
    closePalette();
    return true;
  }
  if (event.key === "ArrowDown") {
    event.preventDefault();
    active = Math.min(items.length - 1, active + 1);
    highlight();
    return true;
  }
  if (event.key === "ArrowUp") {
    event.preventDefault();
    active = Math.max(0, active - 1);
    highlight();
    return true;
  }
  if (event.key === "Enter") {
    event.preventDefault();
    run(active);
    return true;
  }
  return false;
}

/* One keydown handler for the whole page.
 *
 * A global handler rather than per-element listeners because half these keys
 * have to work while the caret is in the composer -- and because "which element
 * had focus" is the wrong question for a shortcut that means the same thing
 * everywhere. */
export function bindKeys() {
  const input = $("palette-input");
  input.addEventListener("input", () => {
    query = input.value;
    renderPalette();
  });
  input.addEventListener("keydown", (event) => paletteKeydown(event));

  document.addEventListener("keydown", (event) => {
    if (paletteOpen()) return;

    const mod = event.metaKey || event.ctrlKey;
    const target = event.target;
    // An empty composer is not "typing": someone who has not written anything
    // and presses `?` is asking for help, not typing a question mark. The
    // character is suppressed, which is the only way a shortcut can be
    // discoverable from the empty state that advertises it.
    const emptyComposer =
      target instanceof HTMLElement &&
      target.id === "goal" &&
      target.value === "" &&
      !target.selectionStart;
    const typing =
      target instanceof HTMLElement &&
      (target.tagName === "INPUT" || target.tagName === "TEXTAREA") &&
      target.offsetParent !== null &&
      !emptyComposer;

    if (mod && event.key.toLowerCase() === "k") {
      event.preventDefault();
      openPalette("");
      return;
    }
    if (mod && event.key.toLowerCase() === "n") {
      event.preventDefault();
      emit("command", "new-task");
      return;
    }
    if (mod && event.shiftKey && event.key.toLowerCase() === "e") {
      event.preventDefault();
      emit("command", "export");
      return;
    }
    if (mod && event.shiftKey && event.key.toLowerCase() === "d") {
      event.preventDefault();
      emit("command", "density");
      return;
    }
    if (event.key === "Escape") {
      if (helpOpen()) {
        closeHelp();
        return;
      }
      emit("command", "escape");
      return;
    }
    if (event.key === "?" && !typing) {
      event.preventDefault();
      openHelp();
      return;
    }
    if (event.key === "/" && !typing) {
      event.preventDefault();
      openPalette("/");
    }
  });
}

export { KEY };
