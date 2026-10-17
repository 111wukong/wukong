/* Undoing a task's file changes.
 *
 * The runtime could already do this from the CLI, and the console could not,
 * which meant the one destructive action in the system was also the one with
 * no visible preview. So the preview comes first and is the whole feature:
 * a list of files, what will happen to each, and a button that is disabled
 * until the list has been read.
 *
 * It is not a checkpoint timeline. A rewind restores the state *before the
 * task*, which is the question people actually ask ("undo this run"), and
 * offering per-step restoration would be a promise about intermediate states
 * that the checkpoint format cannot keep.
 */

import { json, post } from "./api.js";
import { $, el, emit, state } from "./store.js";
import { bytes } from "./format.js";

const ACTION_LABEL = {
  restore: "还原",
  delete: "删除",
  already: "未改动",
  blocked: "无法还原",
};

export async function openRewind(taskId) {
  if (!taskId) {
    emit("toast", { text: "还没有选中的任务", tone: "err" });
    return;
  }
  let plan;
  try {
    plan = await json(`/api/v1/tasks/${taskId}/rewind`);
  } catch (error) {
    emit("toast", { text: `读取失败：${error.message || error}`, tone: "err" });
    return;
  }

  const host = state.run?.assistant || $("transcript");
  const card = el("section", "rewind-card");
  const head = el("div", "rewind-head");
  head.append(el("span", "rewind-title", "还原这次任务改过的文件"));
  head.append(
    el("span", `chip ${plan.actionable ? "chip-warn" : ""}`.trim(), `${plan.actionable} 个文件`),
  );
  card.append(head);

  if (!plan.steps.length) {
    card.append(el("div", "rewind-note", "这个任务没有记录任何文件检查点，没有可还原的内容。"));
    host.append(card);
    emit("scroll");
    return;
  }

  const list = el("div", "rewind-list");
  for (const step of plan.steps) {
    const row = el("div", `rewind-row ${step.action}`);
    row.append(el("span", "rewind-action", ACTION_LABEL[step.action] || step.action));
    row.append(el("span", "rewind-path", step.path));
    const detail =
      step.action === "delete"
        ? "任务创建的文件"
        : step.action === "blocked"
          ? step.reason || "没有快照"
          : step.bytes
            ? `${bytes(step.bytes)}`
            : "";
    row.append(el("span", "rewind-detail", detail));
    list.append(row);
  }
  card.append(list);

  const foot = el("div", "rewind-foot");
  const note = el("span", "rewind-note", "还原会覆盖磁盘上的当前内容。");
  const actions = el("div", "review-actions");
  const cancel = el("button", "btn btn-ghost", "取消");
  cancel.type = "button";
  const go = el("button", "btn btn-danger", `还原 ${plan.actionable} 个文件`);
  go.type = "button";
  go.disabled = plan.actionable === 0;
  actions.append(cancel, go);
  foot.append(note, actions);
  card.append(foot);

  cancel.onclick = () => card.remove();
  go.onclick = async () => {
    go.disabled = true;
    go.textContent = "还原中…";
    try {
      const result = await post(`/api/v1/tasks/${taskId}/rewind`, {});
      card.classList.add("settled");
      foot.replaceChildren(
        el("span", "rewind-note", `已还原 ${result.restored} 个文件。`),
      );
      emit("toast", { text: `已还原 ${result.restored} 个文件`, tone: "ok" });
      emit("files-changed");
    } catch (error) {
      go.disabled = false;
      go.textContent = "重试";
      emit("toast", { text: `还原失败：${error.message || error}`, tone: "err" });
    }
  };

  host.append(card);
  emit("scroll");
}
