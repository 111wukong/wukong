/* Unified diffs, as structure and as DOM.
 *
 * The console gets diffs from three places: a tool's `preview`, a tool result
 * that happens to be a `git diff`, and the review pane's changeset. They all
 * arrive as text, so they all go through the same parser -- one place to fix
 * when a diff has a shape the colouriser did not expect.
 */

import { el } from "./md.js";

export function looksLikeDiff(text) {
  const lines = String(text ?? "").split("\n").slice(0, 40);
  const hasHeader = lines.some((line) => line.startsWith("+++") || line.startsWith("---"));
  const hasChange = lines.some((line) => /^[+-][^+-]/.test(line));
  return hasHeader && hasChange;
}

/**
 * A diff as lines with both line numbers.
 *
 * Numbers are computed by walking, not read out of the `@@` header: the header
 * gives a start, and a gutter needs every line. Doing that arithmetic twice is
 * where an off-by-one hides.
 */
export function parseDiff(text) {
  const out = [];
  let oldNo = 0;
  let newNo = 0;
  let inHunk = false;

  for (const raw of String(text ?? "").split("\n")) {
    if (raw.startsWith("@@")) {
      const match = /@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(raw);
      oldNo = match ? Number(match[1]) : 0;
      newNo = match ? Number(match[2]) : 0;
      inHunk = true;
      out.push({ kind: "hunk", text: raw, oldNo: null, newNo: null });
      continue;
    }
    if (/^(\+\+\+|---)/.test(raw)) {
      out.push({ kind: "meta", text: raw, oldNo: null, newNo: null });
      continue;
    }
    if (raw.startsWith("\\")) {
      out.push({ kind: "meta", text: raw, oldNo: null, newNo: null });
      continue;
    }
    if (!inHunk) continue;

    const marker = raw[0];
    const body = raw.slice(1);
    if (marker === "+") {
      out.push({ kind: "add", text: body, oldNo: null, newNo: newNo++ });
    } else if (marker === "-") {
      out.push({ kind: "del", text: body, oldNo: oldNo++, newNo: null });
    } else {
      out.push({ kind: "ctx", text: body, oldNo: oldNo++, newNo: newNo++ });
    }
  }
  return out;
}

const CLASS = {
  meta: "diff-meta",
  hunk: "diff-hunk",
  add: "diff-add",
  del: "diff-del",
  ctx: "diff-ctx",
};

export function renderDiff(text) {
  const box = el("div", "diff");
  for (const line of parseDiff(text)) {
    const row = el("div", `diff-line ${CLASS[line.kind] || "diff-ctx"}`);
    // The gutter shows the number of the side the line belongs to. A deleted
    // line has no new number and an added line has no old one -- showing the
    // other side's number there is the single most confusing thing a diff
    // viewer can do.
    const no = line.kind === "del" ? line.oldNo : line.newNo;
    row.append(el("span", "diff-no", no === null || no === undefined ? "" : String(no)));
    row.append(el("span", "diff-text", line.text));
    box.append(row);
  }
  return box;
}

/** A tool result that is a diff gets the diff treatment; anything else stays
 *  plain text. Colourising prose produces a wall of one colour, which is
 *  worse than no colour at all. */
export function renderOutput(text) {
  const body = String(text ?? "");
  if (looksLikeDiff(body)) {
    const wrap = el("div", "tool-result diff");
    wrap.append(renderDiff(body));
    return wrap;
  }
  return el("pre", "tool-result", body.slice(0, 12000));
}
