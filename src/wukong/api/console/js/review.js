/* The review pane.
 *
 * An approval prompt that shows a diff and offers one Yes/No button is not a
 * review; it is a formality. The reviewer's actual question is "which parts of
 * this are right?", and the answer has to be expressible, or the only honest
 * options are "all" and "none".
 *
 * So this pane renders the changeset the server computed (files → hunks →
 * lines) with a checkbox per hunk, and sends back the ids that were kept. The
 * server rebuilds the file from exactly those hunks before writing, which is
 * why a rejected hunk is absent rather than undone: it never existed.
 */

import { json, post } from "./api.js";
import { $, el, emit, setText } from "./store.js";

let current = null; // { taskId, interrupt, review }
const selected = new Set();

export function isOpen() {
  return !$("review").hidden;
}

export function closeReview() {
  $("review").hidden = true;
  $("app").classList.remove("with-review");
  current = null;
  selected.clear();
}

/** Open the pane for a pending call, fetching the changeset if needed.
 *
 * The interrupt usually carries `review` already -- computing it at pause time
 * means the pane opens without a round trip while the run is stopped. The
 * fetch is the fallback for an older event, or for a task paused before this
 * page was open.
 */
export async function openReview(taskId, interrupt) {
  current = { taskId, interrupt, review: interrupt?.review || null };
  $("review").hidden = false;
  $("app").classList.add("with-review");
  $("review-files").replaceChildren(el("div", "preview-note", "读取改动…"));
  setText($("review-title"), interrupt?.tool ? `${interrupt.tool} · 待批准` : "改动审查");
  setText($("review-meta"), "");

  if (!current.review) {
    try {
      const payload = await json(`/api/v1/tasks/${taskId}/review`);
      current.review = payload.review;
    } catch (error) {
      $("review-files").replaceChildren(
        el("div", "preview-note error", `读取失败：${error.message || error}`),
      );
      return;
    }
  }

  selected.clear();
  for (const file of current.review?.files || []) {
    for (const hunk of file.hunks || []) selected.add(hunk.id);
  }
  render();
}

function render() {
  const review = current?.review;
  const list = $("review-files");
  list.replaceChildren();

  if (!review || !(review.files || []).length) {
    list.append(
      el("div", "preview-note", review?.reason || "这次调用没有可拆分的文件改动。"),
    );
    setText($("review-meta"), "无文件改动");
    $("review-accept").textContent = "批准";
    $("review-note").textContent = "";
    return;
  }

  const files = review.files;
  const totalAdd = files.reduce((sum, file) => sum + (file.additions || 0), 0);
  const totalDel = files.reduce((sum, file) => sum + (file.deletions || 0), 0);
  setText($("review-meta"), `${files.length} 个文件 · +${totalAdd} −${totalDel}`);

  for (const file of files) {
    list.append(renderFile(file, review.partial));
  }

  // The note explains what the checkboxes can and cannot do. A control whose
  // limits are invisible is a control that lies once and is never trusted
  // again.
  $("review-note").textContent = review.partial
    ? "取消勾选的改动块不会被写入 —— 服务端只把勾选的部分写进文件。"
    : review.reason || "这次调用只能整体接受或拒绝。";
  updateFooter();
}

function renderFile(file, partial) {
  const box = el("div", "review-file");
  const head = el("div", "review-file-head");
  head.append(el("span", "review-file-status", statusWord(file.status)));
  head.append(el("span", "review-file-path", file.path));
  const stat = el("span", "review-file-stat");
  stat.append(el("span", "add", `+${file.additions || 0}`));
  stat.append(document.createTextNode(" "));
  stat.append(el("span", "del", `−${file.deletions || 0}`));
  head.append(stat);
  box.append(head);

  if (!(file.hunks || []).length) {
    const note = el("div", "preview-note", file.status === "create" ? "新文件，内容整体写入。" : "没有可显示的差异。");
    box.append(note);
    return box;
  }

  for (const hunk of file.hunks) {
    box.append(renderHunk(hunk, partial));
  }
  return box;
}

function statusWord(status) {
  if (status === "create") return "新建";
  if (status === "append") return "追加";
  if (status === "delete") return "删除";
  return "修改";
}

function renderHunk(hunk, partial) {
  const box = el("div", "hunk");
  box.dataset.hunk = hunk.id;

  const head = el("label", "hunk-head");
  const check = document.createElement("input");
  check.type = "checkbox";
  // Without a partial path the box is a label, not a control: it shows what
  // will be written, and it is checked because all of it will be.
  check.checked = partial ? selected.has(hunk.id) : true;
  check.disabled = !partial;
  check.onchange = () => {
    if (check.checked) selected.add(hunk.id);
    else selected.delete(hunk.id);
    box.classList.toggle("rejected", !check.checked);
    updateFooter();
  };
  head.append(check);
  head.append(el("span", "", hunk.header || ""));
  head.append(el("span", "hunk-id", hunk.id));
  box.append(head);

  const diff = el("div", "diff");
  for (const line of hunk.lines || []) {
    const kind = line.kind === "add" ? "diff-add" : line.kind === "del" ? "diff-del" : "diff-ctx";
    const row = el("div", `diff-line ${kind}`);
    const no = line.kind === "del" ? line.old_no : line.new_no;
    row.append(el("span", "diff-no", no === null || no === undefined ? "" : String(no)));
    row.append(el("span", "diff-text", line.text));
    diff.append(row);
  }
  box.append(diff);
  if (hunk.truncated) {
    box.append(el("div", "preview-note", "这一块过长，只显示了前 400 行。"));
  }
  if (partial && !selected.has(hunk.id)) box.classList.add("rejected");
  return box;
}

function updateFooter() {
  const review = current?.review;
  const all = [];
  for (const file of review?.files || []) for (const hunk of file.hunks || []) all.push(hunk.id);

  const total = all.length;
  const checked = all.filter((id) => selected.has(id)).length;
  const allBox = $("review-all");
  allBox.checked = total > 0 && checked === total;
  allBox.indeterminate = checked > 0 && checked < total;
  allBox.disabled = !review?.partial;

  const button = $("review-accept");
  button.textContent =
    total && checked === 0 ? "全部拒绝" : total && checked < total ? `接受 ${checked}/${total} 块` : "接受全部";
  button.disabled = false;
}

export async function acceptReview() {
  if (!current) return;
  const { taskId, review } = current;
  const all = [];
  for (const file of review?.files || []) for (const hunk of file.hunks || []) all.push(hunk.id);
  const keep = all.filter((id) => selected.has(id));

  if (all.length && keep.length === 0) {
    await denyReview();
    return;
  }

  const body = review?.partial && keep.length !== all.length ? { accept_hunks: keep } : {};
  closeReview();
  emit("review-decided", "approve");
  try {
    await post(`/api/v1/tasks/${taskId}/approve`, body);
    emit("resume", taskId);
  } catch (error) {
    emit("error", `批准失败：${error.message || error}`);
  }
}

export async function denyReview() {
  if (!current) return;
  const { taskId } = current;
  closeReview();
  emit("review-decided", "deny");
  try {
    await post(`/api/v1/tasks/${taskId}/deny`, {});
    emit("resume", taskId);
  } catch (error) {
    emit("error", `拒绝失败：${error.message || error}`);
  }
}

export function selectAll(value) {
  const review = current?.review;
  selected.clear();
  if (value) {
    for (const file of review?.files || []) {
      for (const hunk of file.hunks || []) selected.add(hunk.id);
    }
  }
  for (const box of $("review-files").querySelectorAll(".hunk")) {
    const input = box.querySelector('input[type="checkbox"]');
    if (input) input.checked = value;
    box.classList.toggle("rejected", !value);
  }
  updateFooter();
}

