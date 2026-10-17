"""Hunk-level review of a write that has not happened yet.

The approval prompt already shows a unified diff, and that turned out not to
be enough. Showing is not reviewing: a five-file change arrives as one
Yes/No button, so "approve" degenerates into "skim the colours and click".
The question a reviewer actually has is narrower than the file and wider than
the diff -- *which parts of this are right?* -- and a single button cannot
answer it.

So the same diff is turned into a structure: files, hunks, line numbers, and
a stable id per hunk. The client renders checkboxes; the server takes the ids
back and recomputes the file from the hunks that were accepted.

**Why recompute instead of patching the diff.** The ledger's guarantee is
that nothing is written before approval, which means the pending call is still
just a `content` string in memory. Changing which hunks are accepted therefore
only changes that string -- one ordinary write, no second patch engine, no
partially-written file to roll back. A rejected hunk is not "undone"; it never
existed.

**What is reviewable.** `write_file` in overwrite mode, against an existing
file: hunks are independent slices of one known `before`, so any subset is a
valid file. Creating a file is one hunk by definition, and `apply_patch`'s
edits are already the unit of intent, so those are reviewed per edit and the
whole call is accepted or denied. Claiming hunk-level control where it is not
real would be worse than not offering it.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any, Iterable

#: The context width the tool previews use. Must match `tools.fs._unified_diff`
#: or the hunk ids a reviewer saw would not line up with the ones approved.
DIFF_CONTEXT = 3

#: A review pane is not a pager. Beyond this the hunk is truncated and the
#: client offers the file itself, which it can already render.
MAX_LINES_PER_HUNK = 400
#: Likewise for the number of files in one changeset.
MAX_FILES = 20


def _label(raw: str, workspace: Path) -> str:
    path = Path(raw)
    target = path if path.is_absolute() else workspace / path
    try:
        return str(target.resolve().relative_to(workspace.resolve()))
    except (ValueError, OSError):
        return str(target)


def _resolve(raw: str, workspace: Path) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else workspace / path


def parse_unified(text: str) -> list[dict[str, Any]]:
    """Split a unified diff into hunks with per-line numbers.

    Line numbers are computed rather than parsed out of the `@@` header,
    because the header only gives the start: a reviewer looking at a gutter
    needs the number of every line, and a second arithmetic pass over the
    header is exactly where an off-by-one hides.
    """
    hunks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    old_no = new_no = 0

    for raw in text.splitlines():
        if raw.startswith("@@"):
            header = raw
            try:
                body = header[2:].split("@@", 1)[0].strip()
                old_part, new_part = body.split(" ")
                old_start = int(old_part[1:].split(",")[0])
                old_len = int(old_part.split(",")[1]) if "," in old_part else 1
                new_start = int(new_part[1:].split(",")[0])
                new_len = int(new_part.split(",")[1]) if "," in new_part else 1
            except (ValueError, IndexError):
                old_start = old_len = new_start = new_len = 0
            current = {
                "id": f"h{len(hunks)}",
                "header": header,
                "old_start": old_start,
                "old_len": old_len,
                "new_start": new_start,
                "new_len": new_len,
                "lines": [],
                "truncated": False,
            }
            hunks.append(current)
            old_no, new_no = old_start, new_start
            continue

        if current is None:
            # The `---`/`+++` preamble. Not a line of the change.
            continue
        if raw.startswith("\\"):
            # `\ No newline at end of file`. Carries no content.
            continue

        marker, body = (raw[:1], raw[1:]) if raw[:1] in (" ", "+", "-") else (" ", raw)
        if marker == " ":
            kind = "ctx"
        elif marker == "+":
            kind = "add"
        else:
            kind = "del"

        if len(current["lines"]) >= MAX_LINES_PER_HUNK:
            current["truncated"] = True
            continue

        current["lines"].append(
            {
                "kind": kind,
                "text": body,
                "old_no": old_no if kind in ("ctx", "del") else None,
                "new_no": new_no if kind in ("ctx", "add") else None,
            }
        )
        if kind in ("ctx", "del"):
            old_no += 1
        if kind in ("ctx", "add"):
            new_no += 1

    return hunks


def diff_hunks(before: str, after: str, *, context: int = DIFF_CONTEXT) -> list[dict[str, Any]]:
    """Hunks for two whole texts, numbered as a unified diff would number them."""
    lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile="a",
            tofile="b",
            lineterm="",
            n=context,
        )
    )
    if not lines:
        return []
    return parse_unified("\n".join(lines))


def _old_index(hunk: dict[str, Any]) -> int:
    """0-based index into the old file where this hunk starts.

    A pure insertion (`old_len == 0`) is reported by difflib as starting
    *after* line `old_start`, so subtracting one would insert a line early --
    and only for the first hunk, which is the kind of bug that survives a
    review and shows up as a mysteriously missing blank line.
    """
    return hunk["old_start"] - 1 if hunk["old_len"] else hunk["old_start"]


def apply_accepted(
    before: str,
    after: str,
    accepted: Iterable[str] | None,
    *,
    context: int = DIFF_CONTEXT,
) -> str:
    """The file that results from accepting only some hunks.

    `accepted=None` means all of them, which is the ordinary approval and
    returns `after` unchanged -- not a reconstruction of it. Reconstructing
    when there is nothing to decide would put this module in the path of every
    write for no reason.
    """
    if accepted is None:
        return after

    wanted = set(accepted)
    hunks = diff_hunks(before, after, context=context)
    if not hunks:
        return after

    before_lines = before.splitlines()
    out: list[str] = []
    cursor = 0
    last_hunk: dict[str, Any] | None = None
    last_accepted = True

    for hunk in hunks:
        start = _old_index(hunk)
        out.extend(before_lines[cursor:start])
        keep_new = hunk["id"] in wanted
        for line in hunk["lines"]:
            if line["kind"] == "ctx":
                out.append(line["text"])
            elif line["kind"] == "add":
                if keep_new:
                    out.append(line["text"])
            else:  # del
                if not keep_new:
                    out.append(line["text"])
        cursor = start + hunk["old_len"]
        last_hunk, last_accepted = hunk, keep_new

    out.extend(before_lines[cursor:])

    body = "\n".join(out)
    if not body:
        return ""
    # The tail decides the trailing newline: a file that ends without one must
    # not gain one just because a hunk was rejected.
    touches_eof = last_hunk is not None and (
        last_hunk["new_start"] + max(last_hunk["new_len"], 0) - 1 >= len(after.splitlines())
    )
    if touches_eof:
        newline = after.endswith("\n") if last_accepted else before.endswith("\n")
    else:
        newline = before.endswith("\n")
    return body + ("\n" if newline else "")


def _write_file_changeset(arguments: dict[str, Any], workspace: Path) -> dict[str, Any]:
    raw = str(arguments.get("path") or "")
    label = _label(raw, workspace)
    target = _resolve(raw, workspace)
    mode = str(arguments.get("mode") or "overwrite")
    content = str(arguments.get("content") or "")

    if mode == "append":
        return {
            "tool": "write_file",
            "reviewable": False,
            "partial": False,
            "reason": "追加写入没有可拆分的改动单元",
            "additions": len(content.splitlines()),
            "deletions": 0,
            "files": [
                {
                    "path": label,
                    "status": "append",
                    "additions": len(content.splitlines()),
                    "deletions": 0,
                    "hunks": [],
                }
            ],
        }

    try:
        before = target.read_text(encoding="utf-8")
        exists = True
    except (OSError, UnicodeDecodeError):
        before = ""
        exists = False

    if not exists:
        lines = content.splitlines()
        return {
            "tool": "write_file",
            "reviewable": False,
            "partial": False,
            "reason": "新文件整体是一个改动，无法逐块取舍",
            "additions": len(lines),
            "deletions": 0,
            "files": [
                {
                    "path": label,
                    "status": "create",
                    "additions": len(lines),
                    "deletions": 0,
                    "hunks": [],
                }
            ],
        }

    hunks = diff_hunks(before, content)
    additions = sum(1 for h in hunks for line in h["lines"] if line["kind"] == "add")
    deletions = sum(1 for h in hunks for line in h["lines"] if line["kind"] == "del")
    truncated = any(h["truncated"] for h in hunks)
    return {
        "tool": "write_file",
        "reviewable": bool(hunks) and not truncated,
        "partial": bool(hunks) and not truncated,
        "reason": (
            "改动过大，只能整体接受或拒绝" if truncated else ""
        ),
        "additions": additions,
        "deletions": deletions,
        "files": [
            {
                "path": label,
                "status": "modify",
                "additions": additions,
                "deletions": deletions,
                "hunks": hunks,
            }
        ],
    }


def _apply_patch_changeset(arguments: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """One hunk per edit, because an edit is already the unit of intent."""
    raw = str(arguments.get("path") or "")
    label = _label(raw, workspace)
    target = _resolve(raw, workspace)
    try:
        # Read only to prove the file is readable and text: the hunks below are
        # computed per edit, not from a whole-file diff.
        target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {
            "tool": "apply_patch",
            "reviewable": False,
            "partial": False,
            "reason": "读不到当前文件，无法预览改动",
            "additions": 0,
            "deletions": 0,
            "files": [],
        }

    edits = arguments.get("edits") or []
    hunks: list[dict[str, Any]] = []
    additions = deletions = 0
    for index, edit in enumerate(edits):
        old = str(edit.get("old") or "")
        new = str(edit.get("new") or "")
        if not old:
            continue
        single = diff_hunks(old, new, context=2)
        lines = [line for hunk in single for line in hunk["lines"]]
        additions += sum(1 for line in lines if line["kind"] == "add")
        deletions += sum(1 for line in lines if line["kind"] == "del")
        hunks.append(
            {
                "id": f"e{index}",
                "header": f"@@ 编辑 {index + 1} @@",
                "old_start": 0,
                "old_len": len(old.splitlines()),
                "new_start": 0,
                "new_len": len(new.splitlines()),
                "lines": lines,
                "truncated": len(lines) > MAX_LINES_PER_HUNK,
            }
        )

    return {
        "tool": "apply_patch",
        "reviewable": False,
        "partial": False,
        "reason": "逐条编辑由工具原子应用，接受或拒绝整次调用",
        "additions": additions,
        "deletions": deletions,
        "files": [
            {
                "path": label,
                "status": "modify",
                "additions": additions,
                "deletions": deletions,
                "hunks": hunks[:MAX_FILES],
            }
        ],
    }


def changeset(tool: str, arguments: dict[str, Any], workspace: Path) -> dict[str, Any] | None:
    """A reviewable description of the pending write, or None if it is not one.

    None rather than an empty structure: the approval prompt is also used for
    commands and network calls, and a review pane that appears empty on those
    reads as broken rather than as "not applicable".
    """
    if tool == "write_file":
        return _write_file_changeset(arguments, workspace)
    if tool == "apply_patch":
        return _apply_patch_changeset(arguments, workspace)
    return None


def revise(arguments: dict[str, Any], accepted: Iterable[str], workspace: Path) -> dict[str, Any]:
    """The arguments that write only the accepted hunks.

    Only `write_file` in overwrite mode is reconstructable. Anything else
    raises rather than silently writing everything: an approval that names
    hunks and then ignores them is worse than one that does not offer the
    choice at all.
    """
    raw = str(arguments.get("path") or "")
    target = _resolve(raw, workspace)
    mode = str(arguments.get("mode") or "overwrite")
    if mode != "overwrite":
        raise ValueError("只有覆盖写入支持逐块接受")
    try:
        before = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("读不到当前文件，无法按块重建") from exc

    content = str(arguments.get("content") or "")
    wanted = list(accepted)
    # An id that is not in the diff is a client bug, and the failure mode of
    # ignoring it is a write that looks narrowed and is not. Refuse instead.
    known = {hunk["id"] for hunk in diff_hunks(before, content)}
    unknown = sorted(set(wanted) - known)
    if unknown:
        raise ValueError(f"未知的改动块：{', '.join(unknown)}")
    revised = apply_accepted(before, content, wanted)
    out = dict(arguments)
    out["content"] = revised
    return out
