/* One file, read-only until asked otherwise.
 *
 * Read-only is the design, not a limitation. The agent may be mid-run holding a
 * file it has already read, and an editor here would open a question nobody has
 * answered -- whose version wins, and what happens to the agent's belief about
 * the file when it loses. A window cannot race anything.
 */

import { json, post } from "./api.js";
import { $, el, emit, setText, state } from "./store.js";
import { bytes } from "./format.js";
import { paintLine } from "./highlight.js";

export async function openPreview(path) {
  const body = $("preview-body");
  $("preview").hidden = false;
  $("app").classList.add("with-preview");
  setText($("preview-path"), path);
  setText($("preview-meta"), "");
  body.replaceChildren(el("div", "preview-note", "读取中…"));

  let payload;
  try {
    payload = await json(`/api/v1/files/content?path=${encodeURIComponent(path)}`);
  } catch (error) {
    state.previewPath = null;
    body.replaceChildren(el("div", "preview-note error", `读取失败：${error.message || error}`));
    renderActions();
    return;
  }

  // The source and the lock are kept here rather than read back out of the DOM:
  // the rendered view has line numbers woven through it, and un-weaving them is
  // a parser where a variable will do.
  state.previewPath = path;
  state.previewText = payload.text || "";
  state.previewSha = payload.sha || null;
  state.previewEditable = !payload.binary && !payload.reason;

  setText($("preview-meta"), bytes(payload.size));
  renderActions();
  if (!payload.text) {
    body.replaceChildren(el("div", "preview-note", payload.reason || "（空文件）"));
    return;
  }
  body.replaceChildren(renderCode(payload.text));
}

function languageOf(path) {
  const ext = String(path).split(".").pop().toLowerCase();
  return ext === path ? "" : ext;
}

export function closePreview() {
  $("preview").hidden = true;
  $("app").classList.remove("with-preview");
  state.previewPath = null;
}

/* Which buttons belong in the header right now.
 *
 * The edit button is disabled while a task is running, and the tooltip says
 * why. A disabled control with no explanation is a bug report waiting to
 * happen -- and this particular disabled control is load-bearing.
 *
 * The count comes from the server, not from the local run flag: the agent is
 * one in-process object, so a run started from the CLI holds the workspace just
 * as much as one started here, and the server is the one that will refuse the
 * write. Consulting the local flag would show an enabled button for a save that
 * cannot land.
 */
export function renderActions(editing = false) {
  const busy = state.runningTasks > 0;
  const canEdit = Boolean(state.previewPath) && state.previewEditable;

  const edit = $("preview-edit");
  edit.hidden = editing || !canEdit;
  edit.disabled = busy;
  edit.title = busy ? "有任务正在运行，先停下来再编辑" : "编辑这个文件";

  $("preview-save").hidden = !editing;
  $("preview-cancel").hidden = !editing;
  $("preview-edit").hidden = editing || !canEdit;
}

function renderCode(text) {
  const pre = el("pre", "code-view");
  const gutter = el("span", "code-gutter");
  const body = el("span", "code-body");
  const lang = languageOf(state.previewPath || "");
  const lines = text.split("\n");
  for (let i = 0; i < lines.length; i += 1) {
    gutter.append(el("span", "code-ln", String(i + 1)));
    const line = el("span", "code-line");
    paintLine(line, lines[i] || " ", lang);
    body.append(line);
  }
  pre.append(gutter, body);
  return pre;
}

export function startEditing() {
  if (!state.previewPath) return;
  const body = $("preview-body");
  const area = el("textarea", "preview-editor");
  area.value = state.previewText;
  area.spellcheck = false;
  area.onkeydown = (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "s") {
      event.preventDefault();
      saveEditing();
    }
    if (event.key === "Escape") cancelEditing();
  };
  body.replaceChildren(area);
  area.focus();
  renderActions(true);
  $("preview-save").onclick = saveEditing;
  $("preview-cancel").onclick = cancelEditing;
}

export function cancelEditing() {
  if (state.previewPath) openPreview(state.previewPath);
  renderActions(false);
}

export async function saveEditing() {
  const area = $("preview-body").querySelector(".preview-editor");
  if (!area || !state.previewPath) return;
  $("preview-save").disabled = true;
  try {
    const result = await post("/api/v1/files/content", {
      path: state.previewPath,
      text: area.value,
      base_sha: state.previewSha,
    });
    state.previewSha = result.sha;
    state.previewText = area.value;
    emit("toast", { text: "已保存", tone: "ok" });
    await openPreview(state.previewPath);
  } catch (error) {
    emit("toast", { text: `保存失败：${error.message || error}`, tone: "err" });
  } finally {
    $("preview-save").disabled = false;
  }
}
