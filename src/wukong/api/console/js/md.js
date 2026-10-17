/* Markdown → DOM.
 *
 * Hand-rolled rather than pulled in: the project has no build step and no
 * dependencies, and a renderer that builds nodes cannot inject markup the way
 * `innerHTML` can. That second property is not decoration -- model output is
 * untrusted text, and the console renders it next to an approval button.
 *
 * Deliberately partial: fenced code, headings, lists, tables, quotes, rules,
 * inline code / bold / italic / strike / links. Anything else falls through as
 * literal text, which is the right failure mode: a console that mangles a
 * table is worse than one that shows it plainly.
 */

import { paint } from "./highlight.js";

const BLOCK_START = /^(?:```|#{1,4}\s|>\s?|\s*[-*+]\s|\s*\d+[.)]\s|\s*\|)/;

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function renderMarkdown(text) {
  const frag = document.createDocumentFragment();
  const lines = String(text ?? "").split("\n");
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (/^```/.test(line)) {
      const lang = line.slice(3).trim();
      const body = [];
      i += 1;
      while (i < lines.length && !/^```/.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1; // the closing fence, or the end of an unterminated block
      const pre = el("pre", "md-pre");
      if (lang) pre.dataset.lang = lang;
      const code = el("code", "");
      paint(code, body.join("\n"), lang);
      pre.append(code);
      frag.append(pre);
      continue;
    }

    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      const level = heading[1].length;
      const node = el("div", `md-head md-h${level}`);
      appendInline(node, heading[2]);
      frag.append(node);
      i += 1;
      continue;
    }

    if (/^\s*(?:---|\*\*\*|___)\s*$/.test(line)) {
      frag.append(el("hr", "md-hr"));
      i += 1;
      continue;
    }

    // A table is recognised by its separator row, not by a leading pipe: a
    // paragraph that happens to start with `|` is a paragraph.
    if (line.includes("|") && i + 1 < lines.length && /^\s*\|?[\s:-]*-[\s|:-]*$/.test(lines[i + 1])) {
      const rows = [];
      const header = splitRow(line);
      i += 2;
      while (i < lines.length && lines[i].includes("|")) {
        rows.push(splitRow(lines[i]));
        i += 1;
      }
      const table = el("table", "md-table");
      const thead = el("thead", "");
      const headRow = el("tr", "");
      for (const cell of header) {
        const th = el("th", "");
        appendInline(th, cell);
        headRow.append(th);
      }
      thead.append(headRow);
      table.append(thead);
      const tbody = el("tbody", "");
      for (const row of rows) {
        const tr = el("tr", "");
        for (const cell of row) {
          const td = el("td", "");
          appendInline(td, cell);
          tr.append(td);
        }
        tbody.append(tr);
      }
      table.append(tbody);
      frag.append(table);
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const body = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        body.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      const quote = el("blockquote", "md-quote");
      quote.append(renderMarkdown(body.join("\n")));
      frag.append(quote);
      continue;
    }

    if (/^\s*[-*+]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const list = el(ordered ? "ol" : "ul", "md-list");
      while (
        i < lines.length &&
        (/^\s*[-*+]\s+/.test(lines[i]) || /^\s*\d+[.)]\s+/.test(lines[i]))
      ) {
        const item = el("li", "");
        const raw = lines[i].replace(/^\s*(?:[-*+]|\d+[.)])\s+/, "");
        // `- [x] done` is a checkbox in every model's output and reads as
        // noise as literal brackets.
        const task = /^\[([ xX])\]\s+(.*)$/.exec(raw);
        if (task) {
          const box = el("span", "md-task", task[1].toLowerCase() === "x" ? "☑" : "☐");
          item.append(box, document.createTextNode(" "));
          appendInline(item, task[2]);
          item.classList.add("md-task-item");
        } else {
          appendInline(item, raw);
        }
        list.append(item);
        i += 1;
      }
      frag.append(list);
      continue;
    }

    if (!line.trim()) {
      i += 1;
      continue;
    }

    const para = [];
    while (i < lines.length && lines[i].trim() && !BLOCK_START.test(lines[i])) {
      para.push(lines[i]);
      i += 1;
    }
    const node = el("div", "md-p");
    appendInline(node, para.join("\n"));
    frag.append(node);
  }

  return frag;
}

function splitRow(line) {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

/* Inline code is split out first, so `**` inside a span stays literal. */
function appendInline(parent, text) {
  for (const part of String(text).split(/(`[^`]+`)/g)) {
    if (part.length > 2 && part.startsWith("`") && part.endsWith("`")) {
      parent.append(el("code", "md-code", part.slice(1, -1)));
    } else {
      appendEmphasis(parent, part);
    }
  }
}

function appendEmphasis(parent, text) {
  const re = /(\*\*[^*]+\*\*|~~[^~]+~~|\*[^*\n]+\*|\[[^\]]+\]\(https?:\/\/[^\s)]+\)|https?:\/\/[^\s<)]+)/g;
  let last = 0;
  let match;
  while ((match = re.exec(text)) !== null) {
    if (match.index > last) {
      parent.append(document.createTextNode(text.slice(last, match.index)));
    }
    const token = match[0];
    if (token.startsWith("**")) {
      parent.append(el("strong", "", token.slice(2, -2)));
    } else if (token.startsWith("~~")) {
      parent.append(el("del", "", token.slice(2, -2)));
    } else if (token.startsWith("[")) {
      const link = /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/.exec(token);
      const anchor = el("a", "", link[1]);
      anchor.href = link[2];
      anchor.target = "_blank";
      anchor.rel = "noreferrer";
      parent.append(anchor);
    } else if (token.startsWith("http")) {
      const anchor = el("a", "", token);
      anchor.href = token;
      anchor.target = "_blank";
      anchor.rel = "noreferrer";
      parent.append(anchor);
    } else {
      parent.append(el("em", "", token.slice(1, -1)));
    }
    last = match.index + token.length;
  }
  if (last < text.length) {
    parent.append(document.createTextNode(text.slice(last)));
  }
}

/* ---------------------------------------------------------------- streaming
 *
 * Re-parsing the whole message on every token is O(n²) and is exactly how a
 * streaming UI starts dropping frames at around two thousand characters.
 *
 * The fix is not a cleverer parser; it is committing work. A blank line outside
 * a fenced block is a block boundary -- nothing after it can change how the
 * text before it parses. So the message is split into a committed part (parsed
 * once, appended) and a tail (re-parsed each frame, but only the tail). Total
 * work is linear in the length of the message instead of quadratic.
 */
export class StreamingMarkdown {
  constructor(host, onFlush) {
    this.host = host;
    // Called after the DOM is updated, not when the delta arrives. The order
    // matters: a scroll issued on the delta lands before the text has height,
    // so it stops one screen short of the bottom -- and the reader sees the
    // transcript freeze mid-answer.
    this.onFlush = onFlush || null;
    this.raw = "";
    this.committed = 0;
    this.queued = false;
    this.fixed = document.createElement("div");
    this.tail = document.createElement("div");
    host.append(this.fixed, this.tail);
  }

  push(delta) {
    this.raw += delta || "";
    if (this.queued) return;
    this.queued = true;
    requestAnimationFrame(() => {
      this.queued = false;
      this.flush();
    });
  }

  set(text) {
    this.raw = "";
    this.committed = 0;
    this.fixed.replaceChildren();
    this.tail.replaceChildren();
    this.push(text);
  }

  flush() {
    const cut = stableBoundary(this.raw, this.committed);
    if (cut > this.committed) {
      this.fixed.append(renderMarkdown(this.raw.slice(this.committed, cut)));
      this.committed = cut;
    }
    this.tail.replaceChildren(renderMarkdown(this.raw.slice(this.committed)));
    if (this.onFlush) this.onFlush();
  }
}

/** The last index where a top-level block ended, scanning only new text. */
function stableBoundary(text, from) {
  let open = false;
  let best = from;
  let i = from;
  while (i < text.length) {
    const nl = text.indexOf("\n", i);
    if (nl === -1) break;
    const line = text.slice(i, nl);
    if (line.startsWith("```")) open = !open;
    if (!open && line.trim() === "" && nl + 1 < text.length) best = nl + 1;
    i = nl + 1;
  }
  return best;
}
