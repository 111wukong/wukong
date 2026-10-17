/* Attachments: paste or drop an image into the composer, send it with the goal.
 *
 * The bytes stay in the browser until the task is created -- nothing is
 * uploaded before there is a task to attach it to, which is also why the
 * strip is cleared on send and never survives a reload. A page that kept
 * them would be keeping a copy of the user's screenshots in localStorage for
 * no reason the user consented to.
 *
 * Server-side limits (type, size, count) live in wukong.attachments; the
 * checks here are the same ones, duplicated so the feedback is instant. The
 * server remains the authority -- a limit that exists only in the browser is
 * a limit that only honest browsers obey.
 */

import { $, el, emit } from "./store.js";
import { bytes } from "./format.js";

const ALLOWED = ["image/png", "image/jpeg", "image/gif", "image/webp"];
const MAX_BYTES = 5 * 1024 * 1024;
const MAX_IMAGES = 8;

let pending = [];
let listeners = [];

export function onAttachmentsChanged(fn) {
  listeners.push(fn);
}

function changed() {
  for (const fn of listeners) fn(pending);
}

function render() {
  const strip = $("attach-strip");
  if (!strip) return;
  strip.replaceChildren();
  strip.hidden = pending.length === 0;
  if (!pending.length) return;

  for (const item of pending) {
    const chip = el("span", "attach-chip");
    const img = document.createElement("img");
    img.className = "attach-thumb";
    img.src = item.data;
    img.alt = item.name || "附件";
    chip.append(img);
    chip.append(el("span", "attach-name", item.name || "图片"));
    chip.append(el("span", "attach-size", bytes(item.data.length * 0.75)));
    const drop = el("button", "attach-drop", "×");
    drop.type = "button";
    drop.title = "移除这张图片";
    drop.onclick = () => {
      pending = pending.filter((p) => p !== item);
      changed();
    };
    chip.append(drop);
    strip.append(chip);
  }
}

function accept(file) {
  if (pending.length >= MAX_IMAGES) {
    emit("toast", { text: `一次最多附加 ${MAX_IMAGES} 张图片`, tone: "err" });
    return;
  }
  const type = file.type === "image/jpg" ? "image/jpeg" : file.type;
  if (!ALLOWED.includes(type)) {
    emit("toast", { text: `不支持的类型：${file.type || "未知"}`, tone: "err" });
    return;
  }
  if (file.size > MAX_BYTES) {
    emit("toast", { text: `${file.name} 有 ${Math.round(file.size / 1024 / 1024)} MB，超过 5 MB`, tone: "err" });
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    pending.push({ name: file.name, media_type: type, data: String(reader.result) });
    changed();
  };
  reader.onerror = () => emit("toast", { text: `读取失败：${file.name}`, tone: "err" });
  reader.readAsDataURL(file);
}

export function bindAttachments() {
  const box = $("composer-box");
  if (!box || box.dataset.attachBound) return;
  box.dataset.attachBound = "1";

  // Paste. The event fires on whatever inside the box has focus, so it is
  // bound to the box itself; a paste of *text* is left strictly alone.
  box.addEventListener("paste", (event) => {
    const files = [...(event.clipboardData?.files || [])];
    if (!files.length) return;
    event.preventDefault();
    for (const file of files) accept(file);
  });

  // Drag and drop. The dragover must be cancelled or the browser navigates
  // to the image instead of handing it over.
  box.addEventListener("dragover", (event) => {
    if (![...(event.dataTransfer?.items || [])].some((i) => i.kind === "file")) return;
    event.preventDefault();
    box.classList.add("dropping");
  });
  box.addEventListener("dragleave", () => box.classList.remove("dropping"));
  box.addEventListener("drop", (event) => {
    box.classList.remove("dropping");
    const files = [...(event.dataTransfer?.files || [])];
    if (!files.length) return;
    event.preventDefault();
    for (const file of files) accept(file);
  });

  onAttachmentsChanged(render);
  render();
}

export function attachments() {
  return pending;
}

export function clearAttachments() {
  pending = [];
  changed();
}

/* Thumbnails for a turn that already ran.
 *
 * The bytes are no longer here, so each one is fetched from the server by the
 * name it recorded at attach time. Crucial detail: the URL is per-task and
 * the name is server-chosen, so a stale thumbnail renders as a broken image
 * on a deleted attachment and nothing worse.
 */
export function renderAttachments(host, images) {
  if (!images?.length) return;
  const row = el("div", "attach-row");
  for (const meta of images) {
    const img = document.createElement("img");
    img.className = "attach-thumb";
    // Two shapes reach here, and telling them apart is the whole function:
    // a just-sent attachment is still a data URL in memory, while a replayed
    // one is a URL the server serves from the task's attachment directory.
    img.src = meta.url || meta.data;
    img.alt = meta.name || "附件";
    img.loading = "lazy";
    img.title = `${meta.name || "附件"} · ${bytes(meta.bytes || (meta.data || "").length * 0.75)}`;
    row.append(img);
  }
  host.append(row);
}
