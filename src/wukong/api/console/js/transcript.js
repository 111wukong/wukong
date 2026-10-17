/* AG-UI events → DOM.
 *
 * The rule this file follows: the transcript is a *timeline*, not a log. A log
 * is a list of lines that all look alike; a timeline tells you where you are,
 * what is running now, and how long it has been running. That is the whole
 * difference between "the agent is working" and "the agent might be stuck".
 *
 * Two things are deliberately not here: the plan is also mirrored into the
 * activity strip above the transcript (a plan that scrolls out of view is
 * decoration), and tool output is rendered by type rather than as one <pre>
 * (a diff, a terminal and a file read are not the same evidence).
 */

import { $, el, emit, setText, state } from "./store.js";
import { StreamingMarkdown } from "./md.js";
import { looksLikeDiff, renderDiff } from "./diff.js";
import { duration, tokens } from "./format.js";
import { statusLabel } from "./sidebar.js";

/* One glyph per *family*, not per tool.
 *
 * Twenty-two bespoke icons is a drawing exercise; five is a vocabulary you
 * learn in one glance. The point is to let a column of calls be scanned for
 * shape -- a run of document glyphs is reading, a chevron is a side effect --
 * without reading a single tool name. */
const TOOL_ICON = {
  read: "M5 3h7l5 5v13H5zM12 3v5h5",
  write: "M4 20h4L19 9a2 2 0 0 0-3-3L5 17zM14 6l3 3",
  exec: "M5 7l5 5-5 5M13 17h6",
  vcs: "M6 4v16M6 8h9a3 3 0 0 1 3 3v9M18 17l-2-2M18 17l2-2",
  plan: "M4 7h16M4 12h16M4 17h10",
  other: "M12 6v12M6 12h12",
};

const TOOL_FAMILY = [
  [/^(read_file|list_directory|file_info|search_files|grep|recall|list_memory|search_memory)$/, "read"],
  [/^(write_file|apply_patch|delete_file|delete_memory|save_memory|update_plan)$/, "write"],
  [/^run_command$/, "exec"],
  [/^git_/, "vcs"],
  [/^(load_skill|finish)$/, "plan"],
];

function toolFamily(name) {
  for (const [pattern, family] of TOOL_FAMILY) {
    if (pattern.test(name)) return family;
  }
  return "other";
}

function toolIcon(name) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "13");
  svg.setAttribute("height", "13");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.7");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  const path = document.createElementNS(NS, "path");
  path.setAttribute("d", TOOL_ICON[toolFamily(name)]);
  svg.append(path);
  return svg;
}

/* The one argument worth showing on the collapsed line.
 *
 * A tool call is identified by what it acted on -- `read_file orders/calc.py`
 * -- not by its JSON. Showing the JSON is what made a run of eight reads take
 * eight screens. */
const ARG_KEYS = ["path", "command", "target", "url", "pattern", "query", "name", "id"];

//: Tools whose first argument names a file the reader may want to open.
const FILE_TOOLS = /^(read_file|write_file|apply_patch|file_info|delete_file)$/;

function summariseArgs(raw) {
  if (!raw) return "";
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return ""; // still streaming; the full JSON is in the expanded body
  }
  if (!parsed || typeof parsed !== "object") return "";
  for (const key of ARG_KEYS) {
    const value = parsed[key];
    if (typeof value === "string" && value) {
      return value.length > 58 ? `${value.slice(0, 55)}…` : value;
    }
  }
  const first = Object.values(parsed).find((value) => typeof value === "string" && value);
  return first ? String(first).slice(0, 58) : "";
}

/* The gist of a result, for the collapsed row.
 *
 * The first line that has anything in it, because tool output usually leads
 * with the answer and follows with detail. Picking the *last* line would show
 * the trailing blank. */
function summariseResult(content) {
  const text = String(content ?? "").trim();
  if (!text) return "";
  const line = text.split("\n").find((candidate) => candidate.trim()) || "";
  const clean = line.trim().replace(/\s+/g, " ");
  return clean.length > 64 ? `${clean.slice(0, 64)}…` : clean;
}

/* Follow the tail, but only once per frame, and never while the reader is
 * somewhere else. Reading `scrollHeight` on every token is a forced synchronous
 * layout per token -- the classic way to make a streaming UI stutter -- and
 * yanking the view back down while someone is reading is worse than not
 * following at all. */
let scrollQueued = false;
/* Whether the reader wants to be at the tail.
 *
 * Measured from the reader's own scrolling, not from the distance at the
 * moment new text arrives. Those two are not the same question: a long answer
 * makes the distance jump to a screenful in one frame, and asking "is the
 * reader far from the bottom?" at that instant answers "yes" -- so the view
 * stops following exactly when there is the most to follow. */
let stick = true;

function bindStick() {
  const box = $("transcript");
  if (!box || box.dataset.stickBound) return;
  box.dataset.stickBound = "1";

  // Recompute from *user* gestures, never from the scroll event itself.
  // A `scroll` listener also fires for the browser's own scroll anchoring and
  // for our own programmatic jumps, and reading the distance at that moment
  // says "the reader left" when the truth is "the content grew".
  const recompute = () => {
    stick = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
  };
  const afterGesture = () => requestAnimationFrame(recompute);
  box.addEventListener("wheel", afterGesture, { passive: true });
  box.addEventListener("touchmove", afterGesture, { passive: true });
  box.addEventListener("keydown", (event) => {
    if (["PageUp", "PageDown", "Home", "End", "ArrowUp", "ArrowDown"].includes(event.key)) {
      afterGesture();
    }
  });
}

export function scrollToBottom(force = false) {
  bindStick();
  if (scrollQueued) return;
  scrollQueued = true;
  requestAnimationFrame(() => {
    scrollQueued = false;
    const box = $("transcript");
    if (!box) return;
    if (!force && !stick) return;

    /* Pin the tail, then re-pin while the height settles.
     *
     * Turns use `content-visibility: auto`, so an off-screen turn reports an
     * *estimated* height until it renders. Scrolling to `scrollHeight` once
     * therefore lands short: the turns that just came into view grow, the
     * height grows with them, and the view is left a screen above the bottom.
     * Two or three frames of re-pinning is enough and costs nothing. */
    let tries = 0;
    const pin = () => {
      box.scrollTop = box.scrollHeight;
      tries += 1;
      if (tries < 4 && box.scrollHeight - box.scrollTop - box.clientHeight > 2) {
        requestAnimationFrame(pin);
      }
    };
    pin();
  });
}

export function newRun() {
  return {
    assistant: null,
    messageText: new Map(), // messageId -> StreamingMarkdown
    tools: new Map(), // toolCallId -> { node, startedAt, name }
    plan: null,
    planSteps: [],
    interrupted: false,
    usage: 0,
    startedAt: performance.now(),
  };
}

export function addTurn(kind, title) {
  $("empty")?.classList.add("hidden");
  const turn = el("div", `turn ${kind}`);
  if (title) {
    const head = el("div", "turn-head");
    head.append(el("span", "", title));
    turn.append(head);
  }
  $("transcript").append(turn);
  scrollToBottom(true);
  return turn;
}

export function clearTranscript() {
  const empty = $("empty");
  $("transcript").replaceChildren(empty);
  empty?.classList.remove("hidden");
  setActivity("");
}

export function setStatus(key, busy = false) {
  const node = $("status");
  setText(node, statusLabel(key));
  node.dataset.state = key;
  node.classList.toggle("busy", busy);

  // The header chip tracks the live state too. Otherwise it only moves when the
  // task list polls, so the header could say "运行中" for five seconds after the
  // run had already stopped -- and the two status lines on screen would
  // disagree.
  const chip = $("topbar-status");
  if (chip) {
    const tone = statusToneOf(key);
    chip.className = `chip ${tone}`.trim();
    setText(chip, statusLabel(key));
    chip.title = key;
  }
  if (key !== "running" && key !== "planning" && key !== "pending") {
    setActivity("");
  }
}

function statusToneOf(key) {
  const map = {
    completed: "chip-ok",
    failed: "chip-danger",
    cancelled: "chip-danger",
    waiting_confirmation: "chip-warn",
    waiting: "chip-warn",
    orphaned: "chip-warn",
    running: "chip-accent",
    planning: "chip-accent",
    pending: "chip-accent",
  };
  return map[key] || "";
}

/* ------------------------------------------------------------------ activity
 *
 * The strip above the transcript answers one question the transcript cannot:
 * what is happening *right now*. A log only ever answers "what happened". */
let activityTimer = null;
let activitySince = 0;

export function setActivity(text, progress = null) {
  const bar = $("activity");
  if (!bar) return;
  if (!text) {
    bar.hidden = true;
    if (activityTimer) clearInterval(activityTimer);
    activityTimer = null;
    return;
  }
  bar.hidden = false;
  setText($("activity-text"), text);
  activitySince = Date.now();
  const tick = () => {
    const elapsed = Date.now() - activitySince;
    setText($("activity-time"), duration(elapsed));
  };
  tick();
  if (!activityTimer) activityTimer = setInterval(tick, 1000);
  if (progress !== null) {
    $("activity-fill").style.width = `${Math.round(progress * 100)}%`;
  }
}

function noteActivity(text) {
  setActivity(text);
}

/* -------------------------------------------------------------------- render */

export function renderEvent(event) {
  const run = state.run;
  if (!run) return;
  switch (event.type) {
    case "RUN_STARTED":
      // Which task this run is. The stream is the only place the client can
      // learn it -- `threadId` is the client's own id, not the task's. Without
      // this, approving a prompt posted to whatever task the list had selected.
      if (event.taskId) {
        state.taskId = event.taskId;
        emit("task-id", event.taskId);
      }
      run.assistant = addTurn("assistant", "Agent");
      setStatus("running", true);
      setActivity("正在思考…", 0);
      break;

    case "STEP_STARTED":
      run.stepName = event.stepName;
      run.stepStartedAt = performance.now();
      noteActivity(`正在执行 ${event.stepName}`);
      break;

    case "STEP_FINISHED": {
      const elapsed = run.stepStartedAt ? performance.now() - run.stepStartedAt : 0;
      run.lastStepDuration = elapsed;
      run.stepStartedAt = null;
      break;
    }

    case "TEXT_MESSAGE_START": {
      const body = el("div", "body md");
      const stream = new StreamingMarkdown(body, () => scrollToBottom());
      run.assistant?.append(body);
      run.messageText.set(event.messageId, stream);
      scrollToBottom();
      break;
    }

    case "TEXT_MESSAGE_CONTENT": {
      const stream = run.messageText.get(event.messageId);
      if (stream) stream.push(event.delta || "");
      break;
    }

    case "TOOL_CALL_START": {
      // One line per call, expandable. A card per call with its arguments
      // always visible turns a run of eight reads into eight screens of JSON --
      // which is what a log file looks like, not a tool.
      const node = el("details", "tool running");
      const head = el("summary", "tool-head");
      const mark = el("span", "tool-mark", "·");
      const icon = el("span", "tool-icon");
      icon.append(toolIcon(event.toolCallName));
      head.append(mark, icon, el("span", "tool-name", event.toolCallName));
      node.append(head);

      const args = el("pre", "tool-args");
      node.append(args);
      node.dataset.args = "";

      run.assistant?.append(node);
      run.tools.set(event.toolCallId, {
        node,
        name: event.toolCallName,
        startedAt: performance.now(),
      });
      noteActivity(`正在调用 ${event.toolCallName}`);
      scrollToBottom();
      break;
    }

    case "TOOL_CALL_ARGS": {
      const entry = run.tools.get(event.toolCallId);
      if (entry) {
        const node = entry.node;
        node.dataset.args += event.delta || "";
        const args = node.querySelector(".tool-args");
        if (args) args.textContent = node.dataset.args;
        const hint = summariseArgs(node.dataset.args);
        const label = node.querySelector(".tool-arg");
        if (hint) {
          if (label) {
            setText(label, hint);
          } else {
            const span = el("span", "tool-arg", hint);
            // A path in a tool row is the file the agent just touched. Making
            // it a link is the difference between reading about the change and
            // looking at it.
            if (FILE_TOOLS.test(entry.name) && !hint.endsWith("…")) {
              span.classList.add("link");
              span.onclick = (click) => {
                click.preventDefault();
                click.stopPropagation();
                emit("open-file", hint);
              };
            }
            node.querySelector(".tool-head")?.append(span);
          }
        }
      }
      break;
    }

    case "TOOL_CALL_END":
      break;

    case "TOOL_CALL_RESULT": {
      const entry = run.tools.get(event.toolCallId);
      if (entry) {
        const { node, startedAt, name } = entry;
        const ok = !event.metadata || event.metadata.success !== false;
        node.classList.remove("running");
        node.classList.add(ok ? "ok" : "failed");
        setText(node.querySelector(".tool-mark"), ok ? "✓" : "✕");
        const took = startedAt ? performance.now() - startedAt : 0;
        // Sub-5ms is the mock model and cached reads; printing "0ms" on every
        // row is noise that makes the column unreadable.
        if (took >= 5) {
          node.querySelector(".tool-head")?.append(el("span", "tool-time", duration(took)));
        }

        // A one-line gist on the collapsed row. Without it, knowing whether a
        // call found anything meant opening all of them -- which is the thing
        // collapsing was supposed to avoid.
        const gist = summariseResult(event.content);
        if (gist) node.querySelector(".tool-head")?.append(el("span", "tool-gist", gist));

        // Evidence by type: a diff is a diff, a command is a terminal, a read
        // is a file. Rendering all three as one grey <pre> is why a run was
        // unreadable at a glance.
        const body = String(event.content ?? "");
        if (looksLikeDiff(body)) {
          const wrap = el("div", "tool-result diff");
          wrap.append(renderDiff(body));
          node.append(wrap);
        } else {
          const pre = el("pre", name === "run_command" ? "tool-result term" : "tool-result");
          pre.textContent = body.slice(0, 12000);
          node.append(pre);
        }
      }
      break;
    }

    case "ACTIVITY_SNAPSHOT": {
      if (event.activityType !== "PLAN") break;
      const steps = event.content.steps || [];
      run.planSteps = steps;
      if (!run.plan) {
        const wrapper = el("section", "plan-card");
        wrapper.append(el("div", "plan-label", "计划"));
        run.plan = el("div", "plan");
        wrapper.append(run.plan);
        run.assistant?.append(wrapper);
      }
      renderPlan(run);
      break;
    }

    case "RUN_FINISHED": {
      const outcome = event.outcome || { type: "success" };
      if (outcome.type === "interrupt") {
        run.interrupted = true;
        setStatus("waiting", false);
        emit("approval", outcome.interrupts?.[0] || null);
      } else {
        setStatus("idle", false);
        state.running = false;
        const total = performance.now() - run.startedAt;
        finishTurn(run, total);
      }
      $("btn-cancel").disabled = true;
      break;
    }

    case "RUN_ERROR":
      addTurn("error", "错误").append(el("div", "body", event.message || "未知错误"));
      setStatus("failed", false);
      state.running = false;
      $("btn-cancel").disabled = true;
      setActivity("");
      break;

    case "CUSTOM":
      renderCustom(event);
      break;

    default:
      break;
  }
}

function finishTurn(run, elapsedMs) {
  const head = run.assistant?.querySelector(".turn-head");
  if (head && !head.querySelector(".turn-time")) {
    head.append(el("span", "chip", duration(elapsedMs)));
  }
  // Any tool still marked running never got a result: the run ended while it
  // was in flight. Saying so is better than a spinner that never stops.
  for (const { node } of run.tools.values()) {
    if (node.classList.contains("running")) {
      node.classList.remove("running");
      node.classList.add("failed");
      setText(node.querySelector(".tool-mark"), "!");
    }
  }
  setActivity("");
}

const PLAN_MARKS = { pending: "○", running: "◐", completed: "●", failed: "✕", skipped: "—" };

function renderPlan(run) {
  if (!run.plan) return;
  run.plan.replaceChildren();
  const done = run.planSteps.filter((step) => step.status === "completed").length;
  const total = run.planSteps.length;

  const label = run.plan.parentElement?.querySelector(".plan-label");
  if (label) {
    label.replaceChildren(el("span", "", "计划"));
    label.append(el("span", "plan-progress", `${done}/${total}`));
  }

  for (const [index, step] of run.planSteps.entries()) {
    const status = step.status || "pending";
    const row = el("div", `plan-step ${status}`);
    // The glyph carries the status; the number carries the order. Both are
    // fixed-width so the descriptions line up in a column.
    row.append(el("span", "mark", PLAN_MARKS[status] || "○"));
    row.append(el("span", "num", String(index + 1)));
    row.append(el("span", "step-text", step.description || ""));
    run.plan.append(row);
  }

  const running = run.planSteps.find((step) => step.status === "running");
  if (running) {
    setActivity(`第 ${done + 1}/${total} 步 · ${running.description}`, total ? done / total : 0);
  } else if (total) {
    setActivity(`第 ${done}/${total} 步`, total ? done / total : 0);
  }
}

function renderCustom(event) {
  const run = state.run;
  if (!run) return;

  if (event.name === "usage") {
    // One chip that updates, not one per model call. A run makes a call per
    // step, and appending a chip each time buried the turn header under a row
    // of near-identical numbers.
    const value = event.value || {};
    run.usage = (run.usage || 0) + (value.totalTokens || 0);
    const head = run.assistant?.querySelector(".turn-head");
    if (head) {
      let chip = head.querySelector(".usage-chip");
      if (!chip) {
        chip = el("span", "chip usage-chip");
        head.append(chip);
      }
      setText(chip, `${value.model || "?"} · ${tokens(run.usage)} token`);
    }
    emit("usage", value);
  }

  if (event.name === "context_compacted") {
    run.assistant?.append(
      el("div", "dim", `上下文已压缩${event.value?.droppedEntries ? `，丢弃 ${event.value.droppedEntries} 条早期记录` : ""}`),
    );
  }

  if (event.name === "tool_ambiguous") {
    const warn = el("div", "tool failed");
    const head = el("div", "tool-head");
    head.append(el("span", "tool-mark", "!"), el("span", "tool-name", event.value?.tool || "工具"));
    warn.append(head);
    warn.append(
      el(
        "div",
        "dim",
        "这次调用在中断时正在执行，无法确定它是否已经跑过。继续之前请先确认当前状态。",
      ),
    );
    run.assistant?.append(warn);
  }

  if (event.name === "stream_idle") setStatus("waiting", true);
  if (event.name === "stream_notice") setStatus("resyncing", true);
  if (event.name === "budget_exceeded") {
    run.assistant?.append(el("div", "dim", `预算用尽：${event.value?.kind || ""}`));
  }
}

/* Replaying an old task is not a second renderer.
 *
 * `/api/v1/tasks/{id}/stream` replays the encoded event history and then
 * closes, so a past run is the live path with a stream that ends immediately.
 * A separate "history" view would be a thinner copy of this file that drifts
 * from it -- and the drift would only be visible in the one place nobody
 * tests: looking at yesterday's work.
 */
