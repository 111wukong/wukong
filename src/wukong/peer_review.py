"""Peer review of a pending changeset by a read-only reviewer sub-agent.

The human is the only one who can approve a write, but a human looking at a
five-file diff at 2am is a human who clicks "yes" on the colours. So before the
human is asked, a second agent -- strictly read-only, and unable to write or run
anything -- reads the changeset and the relevant files and returns a structured
verdict. The human sees that verdict next to the diff, and decides.

This is the "互相审查" layer. In a delegation, the same mechanism lets a
designated reviewer sub-agent read the changesets its siblings produce, so the
writers are checked by a peer rather than only by the operator. Here it is wired
into the single write gate, which covers both the orchestrator's own writes and
any write-capable sub-agent's, because both funnel through the same pause.

The reviewer is a full `AgentRuntime` run with a purpose-built permission
engine: read-only is allowed, *everything else is denied* (not merely
"unattended"). That matters: a reviewer that could merely pause on a write would
wedge the parent waiting for a human who is not there, and a reviewer that could
write could collide with the very change it is meant to judge. Denying the other
effects makes both impossible, so peer review can never block a task.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from wukong.agent.state import TaskStatus
from wukong.config import PermissionConfig, Settings
from wukong.orchestration.multi_agent import AgentDeps
from wukong.tools.permissions import PermissionEngine
from wukong.types import Decision, EffectClass


#: The three outcomes a review can produce. There is deliberately no
#: "reject" -- the verdict is advice to the human, who holds the only vote.
VALID_VERDICTS = ("approve", "changes_requested", "blocked")


def _reviewer_engine(workspace: Any, home: Any) -> PermissionEngine:
    """A permission engine that allows reading and denies everything else.

    Built per review so the reviewer can never be promoted by a shared engine's
    `cli_approvals` or a run's `approved_effects` into doing something the
    operator did not intend. The reviewer runs in the parent's workspace, so it
    inherits the same paths; only the effect-class table differs.
    """
    config = PermissionConfig()
    config.defaults = {
        EffectClass.READ_ONLY: Decision.ALLOW,
        EffectClass.WRITE_LOCAL: Decision.DENY,
        EffectClass.EXECUTE_LOCAL: Decision.DENY,
        EffectClass.NETWORK: Decision.DENY,
        EffectClass.EXTERNAL_SIDE_EFFECT: Decision.DENY,
        EffectClass.SYSTEM_ADMIN: Decision.DENY,
    }
    return PermissionEngine(config, workspace=workspace, home=home)


@dataclass
class Concern:
    """One thing the reviewer thinks is wrong or worth a human's eye."""

    file: str
    hunk_id: str | None
    severity: str  # "high" | "medium" | "low"
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "hunk_id": self.hunk_id,
            "severity": self.severity,
            "note": self.note,
        }


@dataclass
class PeerReviewVerdict:
    """What the reviewer concluded.

    `parse_error` is true when the reviewer's output could not be read into the
    structured shape. The safe default then is "changes_requested" with the raw
    text attached, so a parse failure never hides a possible concern and never
    silently approves.
    """

    verdict: str
    concerns: list[Concern] = field(default_factory=list)
    summary: str = ""
    model: str = ""
    task_id: str = ""
    parse_error: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "concerns": [c.as_dict() for c in self.concerns],
            "summary": self.summary,
            "model": self.model,
            "task_id": self.task_id,
            "parse_error": self.parse_error,
        }


REVIEWER_GOAL = """\
You are a senior code reviewer performing a PRE-APPROVAL review of a proposed
change. The change has NOT been applied yet. Your job is to catch mistakes a
human reviewer might skim past: incorrect logic, behaviour that was removed but
was still needed, security or correctness regressions, and changes that do not
match the stated goal.

Below is the proposed change, rendered as a structured changeset. You MAY read
the affected files in the workspace to check the change in context. Do NOT try to
modify or run anything -- you are read-only, and any write or command you
attempt will simply fail.

{changeset}

When you have read enough, give your verdict. End your answer with EXACTLY this
block, and put nothing after it:

[verdict]
<one of: approve | changes_requested | blocked>
[/verdict]
[concerns]
- file: <path> | hunk: <id or blank> | severity: <high|medium|low> | note: <what is wrong and why>
- file: <path> | severity: <high|medium|low> | note: <...>
[/concerns]
[summary]
<two or three sentences: what the change does and whether a human should approve
it as-is. If you requested changes or blocked, say what must change.>
[/summary]

Reserve "approve" for changes that are correct and match their goal. Use
"changes_requested" for fixable issues. Use "blocked" for changes that are
wrong, unsafe, or unrelated to the goal.
"""


def render_brief(changeset: dict[str, Any]) -> str:
    """The changeset as readable text for the reviewer's prompt.

    Loses the line numbers a hunk carries (the reviewer can read the file for
    those) but keeps the file paths, statuses and the add/keep/delete shape of
    every line, which is what a reviewer needs to form a judgement.
    """
    files = changeset.get("files") or []
    lines: list[str] = [
        f"Tool: {changeset.get('tool')}",
        f"Additions: {changeset.get('additions', 0)}  Deletions: {changeset.get('deletions', 0)}",
        "",
    ]
    for file in files:
        lines.append(f"## {file.get('path')}  ({file.get('status')})")
        for hunk in file.get("hunks") or []:
            lines.append(f"### {hunk.get('header') or ''}")
            for line in hunk.get("lines") or []:
                mark = {"add": "+", "del": "-", "ctx": " "}.get(line.get("kind"), " ")
                lines.append(f"{mark} {line.get('text', '')}")
        lines.append("")
    return "\n".join(lines)


def _block(text: str, name: str) -> str | None:
    match = re.search(rf"\[{name}\](.*?)\[/{name}\]", text, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else None


def _field(body: str, name: str) -> str:
    match = re.search(rf"\b{name}:\s*([^|]*)", body, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _parse_concerns(raw: str, *, max_concerns: int) -> list[Concern]:
    out: list[Concern] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith("-"):
            continue
        body = line[1:].strip()
        concern = Concern(
            file=_field(body, "file"),
            hunk_id=(_field(body, "hunk") or None),
            severity=(_field(body, "severity") or "medium"),
            note=_field(body, "note") or body,
        )
        out.append(concern)
        if len(out) >= max_concerns:
            break
    return out


def _parse_verdict(text: str, *, max_concerns: int = 10) -> PeerReviewVerdict:
    """Turn the reviewer's free-form answer into a structured verdict.

    Never raises: an unparseable answer degrades to "changes_requested" with the
    raw text kept, so peer review fails open (towards human attention) rather
    than closed (towards silent approval).
    """
    raw = _block(text, "verdict")
    if not raw:
        return PeerReviewVerdict(
            verdict="changes_requested",
            concerns=[],
            summary=(text or "").strip()[:800],
            parse_error=True,
        )

    verdict = raw.strip().lower().split()[0] if raw.strip() else ""
    if verdict not in VALID_VERDICTS:
        # Tolerate prose like "I think we should APPROVE this".
        for candidate in VALID_VERDICTS:
            if candidate in verdict:
                verdict = candidate
                break
        else:
            verdict = "changes_requested"

    summary = _block(text, "summary") or ""
    concerns = _parse_concerns(_block(text, "concerns") or "", max_concerns=max_concerns)
    return PeerReviewVerdict(
        verdict=verdict,
        concerns=concerns,
        summary=summary.strip()[:800],
        parse_error=False,
    )


async def review_changeset(
    deps: AgentDeps,
    changeset: dict[str, Any],
    *,
    session_id: str,
    parent_task_id: str,
    model_alias: str | None = None,
    settings: Settings,
) -> PeerReviewVerdict:
    """Run a read-only reviewer over `changeset` and return its verdict.

    The reviewer is a fresh `AgentRuntime` with its own read-only-deny engine,
    so it cannot write, run, or pause. Any failure -- the model errors, the
    reviewer pauses, the answer is unparseable -- degrades to a "review
    manually" verdict rather than blocking the parent task.
    """
    from wukong.agent.runtime import AgentRuntime

    config = settings.peer_review
    runtime = AgentRuntime(
        settings=settings,
        store=deps.store,
        registry=deps.registry,
        engine=_reviewer_engine(settings.workspace, settings.home),
        models=deps.models,
        skills=deps.skills,
        bus=deps.bus,
        memory=deps.memory,
    )
    goal = REVIEWER_GOAL.format(changeset=render_brief(changeset))

    try:
        result = await runtime.run(
            goal,
            session_id=session_id,
            model_alias=model_alias or config.model or None,
            approved_effects=[EffectClass.READ_ONLY],
            max_steps=config.max_steps_per_agent,
            parent_task_id=parent_task_id,
            reflect=False,
        )
    except Exception as exc:  # peer review must never block a task
        return PeerReviewVerdict(
            verdict="changes_requested",
            concerns=[],
            summary=f"(peer review failed to run: {exc}; review manually)",
            parse_error=True,
        )

    if result.status != TaskStatus.COMPLETED.value:
        # The reviewer paused (it should not, but a disobedient model might) or
        # hit its step budget. Do not leave the parent waiting on it.
        return PeerReviewVerdict(
            verdict="changes_requested",
            concerns=[],
            summary=f"(peer review did not complete: {result.status}; review manually)",
            parse_error=True,
        )

    verdict = _parse_verdict(result.answer or "", max_concerns=config.max_concerns)
    verdict.model = model_alias or config.model or settings.default_model
    verdict.task_id = result.task_id or ""
    return verdict


__all__ = [
    "Concern",
    "PeerReviewVerdict",
    "render_brief",
    "review_changeset",
]
