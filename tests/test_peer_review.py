"""Peer review of pending changesets.

The interesting assertions here are not "does it run". They are:

* the verdict parser degrades safely -- an unparseable answer becomes
  "changes_requested" with the raw text kept, never a silent approval;
* the reviewer is driven by a real `AgentRuntime` offline (no network, no key),
  and it is read-only in fact: its engine denies every effect but reading;
* the verdict rides on `PendingConfirmation` and survives `replay`, so a paused
  task that is rebuilt after a crash still shows the human the peer's verdict.

The human remains the only decision-maker: peer review attaches advice, it does
not change what `approve` or `deny` can do.
"""

from __future__ import annotations

from typing import Any

from tests.conftest import ScriptedModel, ScriptedModels

from wukong.agent.runtime import AgentRuntime
from wukong.agent.state import AgentState, PendingConfirmation, TaskStatus, replay
from wukong.observability.events import Event, EventType
from wukong.orchestration.multi_agent import AgentDeps
from wukong.peer_review import _parse_verdict, render_brief, review_changeset


def _changeset() -> dict[str, Any]:
    """A small but real-shaped reviewable changeset."""
    return {
        "tool": "write_file",
        "reviewable": True,
        "partial": False,
        "reason": "",
        "additions": 1,
        "deletions": 1,
        "files": [
            {
                "path": "app.py",
                "status": "modify",
                "additions": 1,
                "deletions": 1,
                "hunks": [
                    {
                        "id": "h0",
                        "header": "@@ 1,2 1,2 @@",
                        "lines": [
                            {"kind": "ctx", "text": "def add(a, b):", "old_no": 1, "new_no": 1},
                            {"kind": "del", "text": "    return a + b", "old_no": 2, "new_no": None},
                            {"kind": "add", "text": "    return a - b", "old_no": None, "new_no": 2},
                        ],
                    }
                ],
            }
        ],
    }


def _reviewer_block(verdict: str, *, concerns: str = "") -> str:
    return (
        f"[verdict]\n{verdict}\n[/verdict]\n"
        f"[concerns]\n{concerns}[/concerns]\n"
        "[summary]\nNeeds a human eye.\n[/summary]"
    )


class TestParseVerdict:
    def test_approve(self) -> None:
        v = _parse_verdict(_reviewer_block("approve"))
        assert v.verdict == "approve"
        assert v.parse_error is False

    def test_blocked(self) -> None:
        v = _parse_verdict(_reviewer_block("blocked"))
        assert v.verdict == "blocked"

    def test_changes_requested_with_concerns(self) -> None:
        block = _reviewer_block(
            "changes_requested",
            concerns=(
                "- file: app.py | hunk: h0 | severity: high | note: swaps + for -\n"
                "- file: app.py | severity: low | note: no test\n"
            ),
        )
        v = _parse_verdict(block)
        assert v.verdict == "changes_requested"
        assert len(v.concerns) == 2
        assert v.concerns[0].severity == "high"
        assert v.concerns[0].file == "app.py"
        assert v.concerns[0].hunk_id == "h0"
        assert v.concerns[0].note == "swaps + for -"

    def test_missing_verdict_block_falls_back_open(self) -> None:
        # A parse failure must fail TOWARDS human attention, never silent approve.
        v = _parse_verdict("I read the change and cannot put it in the format.")
        assert v.verdict == "changes_requested"
        assert v.parse_error is True
        assert v.summary

    def test_unknown_verdict_maps_to_changes_requested(self) -> None:
        v = _parse_verdict(_reviewer_block("maybe"))
        assert v.verdict == "changes_requested"
        assert v.parse_error is False

    def test_concern_cap_is_respected(self) -> None:
        many = "\n".join(
            f"- file: f{i}.py | severity: low | note: n{i}" for i in range(20)
        )
        v = _parse_verdict(_reviewer_block("changes_requested", concerns=many))
        assert len(v.concerns) == 10  # PeerReviewConfig.max_concerns default


class TestRenderBrief:
    def test_includes_paths_and_markers(self) -> None:
        brief = render_brief(_changeset())
        assert "app.py" in brief
        assert "write_file" in brief
        assert "+     return a - b" in brief
        assert "-     return a + b" in brief


class TestReviewChangesetRunsReviewer:
    """Drive the real reviewer offline -- no network, no API key."""

    async def test_reviewer_returns_structured_verdict(self, scripted, session_id: str) -> None:
        agent, _ = await scripted([])  # parent agent; its own model is never run here
        reviewer_model = ScriptedModel(
            agent.settings.models["scripted"],
            [{"content": _reviewer_block(
                "changes_requested",
                concerns="- file: app.py | hunk: h0 | severity: high | note: flips arithmetic\n",
            )}],
        )
        deps = AgentDeps(
            settings=agent.settings,
            store=agent.store,
            registry=agent.registry,
            engine=agent.engine,
            models=ScriptedModels(reviewer_model),
            skills=agent.skills,
            bus=agent.bus,
            memory=agent.memory,
        )
        parent_tid = agent.store.create_task(session_id=session_id, goal="parent")
        verdict = await review_changeset(
            deps,
            _changeset(),
            session_id=session_id,
            parent_task_id=parent_tid,
            settings=agent.settings,
        )
        assert verdict.verdict == "changes_requested"
        assert len(verdict.concerns) == 1
        assert verdict.concerns[0].severity == "high"
        # A real child task was created for the reviewer.
        assert verdict.task_id
        assert verdict.model == "scripted"

    async def test_incomplete_reviewer_does_not_block(self, scripted, session_id: str) -> None:
        agent, _ = await scripted([])
        # A reviewer whose model never returns a final answer stalls: the
        # step budget ends it, and the parent must not wait on it.
        reviewer_model = ScriptedModel(
            agent.settings.models["scripted"],
            [{"tool_calls": [{"name": "read_file", "arguments": {"path": "app.py"}}]}],
        )
        deps = AgentDeps(
            settings=agent.settings,
            store=agent.store,
            registry=agent.registry,
            engine=agent.engine,
            models=ScriptedModels(reviewer_model),
            skills=agent.skills,
            bus=agent.bus,
            memory=agent.memory,
        )
        parent_tid = agent.store.create_task(session_id=session_id, goal="parent")
        verdict = await review_changeset(
            deps,
            _changeset(),
            session_id=session_id,
            parent_task_id=parent_tid,
            settings=agent.settings,
        )
        # Degrades to "review manually" rather than hanging or silently approving.
        assert verdict.verdict == "changes_requested"
        assert verdict.parse_error is True


class TestRuntimeAttachment:
    """The confirmation gate attaches the verdict and never hard-fails on it."""

    async def test_reviewer_attaches_when_enabled(self, scripted, session_id: str) -> None:
        agent, _ = await scripted([])
        runtime: AgentRuntime = agent.runtime
        # The reviewer gets its own model so it does not consume the (unused)
        # parent script. `_peer_review` reads `self.models` for the reviewer.
        runtime.models = ScriptedModels(
            ScriptedModel(
                agent.settings.models["scripted"],
                [{"content": _reviewer_block(
                    "changes_requested",
                    concerns="- file: app.py | severity: low | note: n\n",
                )}],
            )
        )
        agent.settings.peer_review.enabled = True
        state = AgentState(
            task_id="parent-x", session_id=session_id, goal="g", status=TaskStatus.RUNNING
        )
        peer = await runtime._peer_review(state, _changeset(), session_id=session_id)
        assert peer is not None
        assert peer["verdict"] == "changes_requested"
        assert peer["concerns"]
        # The verdict is recorded on the parent task for visibility.
        assert any(
            e.type is EventType.PEER_REVIEW_COMPLETED
            for e in agent.store.events("parent-x")
        )

    async def test_reviewer_skipped_when_disabled(self, scripted, session_id: str) -> None:
        agent, _ = await scripted([])  # peer_review disabled by default
        state = AgentState(
            task_id="parent-y", session_id=session_id, goal="g", status=TaskStatus.RUNNING
        )
        peer = await agent.runtime._peer_review(state, _changeset(), session_id=session_id)
        assert peer is None


class TestPendingConfirmationField:
    def test_default_is_none(self) -> None:
        pc = PendingConfirmation(
            request_id="r", step_id="s1", tool="write_file", arguments={}
        )
        assert pc.peer_review is None

    def test_replay_reconstructs_peer_review(self) -> None:
        peer = {
            "verdict": "changes_requested",
            "concerns": [],
            "summary": "s",
            "model": "m",
            "task_id": "t",
            "parse_error": False,
        }
        events = [
            Event(
                seq=1,
                task_id="t1",
                type=EventType.TASK_CREATED,
                payload={"goal": "g", "session_id": "s", "budgets": {}},
            ),
            Event(
                seq=2,
                task_id="t1",
                type=EventType.CONFIRMATION_REQUESTED,
                payload={
                    "request_id": "r",
                    "tool": "write_file",
                    "effect": "write_local",
                    "review": {"reviewable": True},
                    "peer_review": peer,
                },
            ),
        ]
        state = replay(events, task_id="t1", session_id="s")
        assert state.pending_confirmation is not None
        assert state.pending_confirmation.peer_review == peer
        # The snapshot that feeds the console must carry it too.
        assert state.snapshot()["pending_confirmation"]["peer_review"] == peer
