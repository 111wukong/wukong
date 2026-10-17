/* The plan gate, rendered where the plan already is.
 *
 * A separate approval pane would put the thing being approved in one column
 * and the decision in another. The plan belongs in the transcript, because
 * that is where the run will be measured against it afterwards -- and because
 * a step you deleted should still be visible as "deleted", not gone from a
 * panel you closed.
 *
 * Editing is deliberately small: keep, drop, reword. Reordering would imply
 * the runtime follows an order it derives from the checklist, and inventing
 * steps is what the model is for.
 */

import { post } from "./api.js";
import { $, el, emit, state } from "./store.js";

let current = null; // { taskId, requestId, steps, card }

export function isOpen() {
  return Boolean(current?.card?.isConnected);
}

/** Render the plan for decision. Idempotent per request id: a replayed stream
 *  reaches this twice (once from the task detail, once from the interrupt) and
 *  two copies of the same plan is a bug the user has to reason about. */
export function renderPlanApproval(interrupt) {
  if (!interrupt) return;
  const steps = Array.isArray(interrupt.plan) ? interrupt.plan : [];
  if (!steps.length) return;
  if (current && current.requestId === interrupt.id && current.card?.isConnected) return;

  const host = state.run?.assistant || $("transcript");
  const card = el("section", "plan-review");

  const head = el("div", "plan-review-head");
  head.append(el("span", "plan-review-title", "执行前确认计划"));
  const count = el("span", "chip chip-warn", `${steps.length} 步`);
  head.append(count);
  card.append(head);

  // The read-only plan card above is the same information, and two copies of
  // a list you are editing is a way to edit the wrong one.
  const readOnly = state.run?.plan?.closest(".plan-card");
  if (readOnly) readOnly.hidden = true;

  const list = el("div", "plan-review-steps");
  const rows = steps.map((step, index) => {
    const row = el("div", "plan-edit");
    const check = document.createElement("input");
    check.type = "checkbox";
    check.checked = true;
    check.setAttribute("aria-label", `执行第 ${index + 1} 步`);
    check.onchange = () => {
      row.classList.toggle("dropped", !check.checked);
      updateCount(count, rows);
    };

    const num = el("span", "plan-edit-num", String(index + 1));
    const text = el("div", "plan-edit-text");
    text.contentEditable = "true";
    text.spellcheck = false;
    text.textContent = step.description || "";
    // A contenteditable that can paste rich text is a contenteditable that
    // ends up holding markup from a web page. Plain text only.
    text.addEventListener("paste", (event) => {
      event.preventDefault();
      const plain = event.clipboardData?.getData("text/plain") || "";
      document.execCommand("insertText", false, plain);
    });

    const drop = el("button", "plan-edit-drop", "×");
    drop.type = "button";
    drop.title = "删除这一步";
    drop.onclick = () => {
      check.checked = false;
      row.classList.add("dropped");
      updateCount(count, rows);
    };

    row.append(check, num, text, drop);
    list.append(row);
    return { row, check, text };
  });
  card.append(list);

  const foot = el("div", "plan-review-foot");
  const note = el("input", "plan-review-note");
  note.placeholder = "哪里不对？可选，会原样告诉 Agent";
  note.spellcheck = false;

  const actions = el("div", "plan-review-actions");
  const reject = el("button", "btn btn-ghost", "让 Agent 重做");
  reject.type = "button";
  const accept = el("button", "btn btn-primary", "按这个计划执行");
  accept.type = "button";
  actions.append(reject, accept);
  foot.append(note, actions);
  card.append(foot);

  accept.onclick = () => {
    const kept = collect(rows);
    if (!kept.length) {
      note.classList.add("shake");
      note.placeholder = "至少保留一个步骤";
      setTimeout(() => note.classList.remove("shake"), 400);
      return;
    }
    settle(card, `已批准 · ${kept.length} 步`);
    send({ plan: kept });
  };
  reject.onclick = () => {
    settle(card, "已退回，等 Agent 重新规划");
    send({ note: note.value.trim() });
  };

  host.append(card);
  current = {
    taskId: state.taskId,
    requestId: interrupt.id,
    steps,
    card,
    readOnly,
  };
  emit("scroll");
}

function updateCount(chip, rows) {
  const kept = rows.filter((row) => row.check.checked).length;
  chip.textContent = kept === rows.length ? `${rows.length} 步` : `${kept}/${rows.length} 步`;
  chip.className = kept ? "chip chip-warn" : "chip chip-danger";
}

function collect(rows) {
  const kept = [];
  for (const { row, check, text } of rows) {
    if (!check.checked) continue;
    const description = (text.textContent || "").replace(/\s+/g, " ").trim();
    if (!description) continue;
    kept.push({ description });
    row.classList.remove("dropped");
  }
  return kept;
}

function settle(card, label) {
  for (const node of card.querySelectorAll("input, button, [contenteditable]")) {
    if (node instanceof HTMLElement) node.setAttribute("contenteditable", "false");
    if ("disabled" in node) node.disabled = true;
  }
  card.classList.add("settled");
  // The plan is no longer a proposal, so the read-only card can carry it.
  if (current?.readOnly) current.readOnly.hidden = false;
  const head = card.querySelector(".plan-review-head");
  if (head) head.append(el("span", "plan-review-done", label));
  const foot = card.querySelector(".plan-review-foot");
  if (foot) foot.remove();
}

async function send(body) {
  if (!current?.taskId) return;
  const taskId = current.taskId;
  const approving = Boolean(body.plan);
  const requestId = current.requestId;
  try {
    if (approving) {
      await post(`/api/v1/tasks/${taskId}/approve`, {
        request_id: requestId,
        plan: body.plan,
      });
    } else {
      await post(`/api/v1/tasks/${taskId}/deny`, { note: body.note || "" });
    }
    emit("plan-decided", { approved: approving });
    emit("resume", taskId);
  } catch (error) {
    emit("error", `计划${approving ? "批准" : "退回"}失败：${error.message || error}`);
  }
}

export function clear() {
  current = null;
}
