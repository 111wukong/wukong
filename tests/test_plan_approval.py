"""Pausing for plan approval.

The gate exists because every other gate fires after the money is spent. What
these tests hold is the two things that make it trustworthy:

* it pauses *before* the first tool call, not after a plan that already ran;
* the steps the human approves are the steps that run -- including the ones
  they deleted, which must not come back because the model's original plan is
  still somewhere in the event log.
"""

from __future__ import annotations

import pytest

from wukong.agent.state import TaskStatus, replay
from wukong.observability.events import EventType


async def _run(agent, session_id: str, *, goal: str = "整理这个项目"):  # noqa: ANN001
    return await agent.runtime.run(
        goal, session_id=session_id, model_alias="scripted", plan_approval=True
    )


class TestTheGate:
    async def test_a_run_pauses_after_planning(self, scripted, session_id: str) -> None:
        agent, _ = await scripted([{"content": "done"}])
        result = await _run(agent, session_id)

        assert result.status == TaskStatus.WAITING_CONFIRMATION.value
        assert result.pending_confirmation is not None
        assert result.pending_confirmation.kind == "plan"

        events = agent.store.events(result.task_id)
        types = [e.type for e in events]
        assert EventType.PLAN_CREATED in types
        # The point of the gate: nothing has run yet. A model call for the
        # plan itself is expected; a tool call is the thing being prevented.
        assert EventType.TOOL_STARTED not in types

    async def test_the_pause_carries_the_steps(self, scripted, session_id: str) -> None:
        agent, _ = await scripted([{"content": "done"}])
        result = await _run(agent, session_id)
        steps = result.pending_confirmation.arguments["steps"]
        assert steps, "a plan pause with no steps is not a decision"
        assert all(step.get("description") for step in steps)

    async def test_without_the_flag_nothing_pauses(self, scripted, session_id: str) -> None:
        agent, _ = await scripted([{"content": "done"}])
        result = await agent.runtime.run(
            "整理这个项目", session_id=session_id, model_alias="scripted"
        )
        assert result.status == TaskStatus.COMPLETED.value

    async def test_attaching_sees_the_plan_as_the_pending_decision(
        self, scripted, session_id: str
    ) -> None:
        """A client that connects after the pause must be able to render it."""
        from wukong.api import agui

        agent, _ = await scripted([{"content": "done"}])
        result = await _run(agent, session_id)
        state = replay(agent.store.events(result.task_id), task_id=result.task_id)
        interrupt = agui.interrupt_for(state.pending_confirmation)
        assert interrupt["kind"] == "plan"
        assert interrupt["plan"] == state.pending_confirmation.arguments["steps"]


class TestTheDecision:
    async def test_approving_runs_the_proposed_steps(self, scripted, session_id: str) -> None:
        agent, _ = await scripted([{"content": "done"}])
        paused = await _run(agent, session_id)
        proposed = paused.pending_confirmation.arguments["steps"]

        result = await agent.runtime.approve(paused.task_id)

        assert result.status == TaskStatus.COMPLETED.value
        state = replay(agent.store.events(paused.task_id), task_id=paused.task_id)
        assert state.plan_approved is True
        assert [s.description for s in state.plan] == [s["description"] for s in proposed]

        # Approving a plan must not try to *execute* it. The "plan" pseudo-tool
        # does not exist, and running it produced a TOOL_FAILED that the model
        # then had to reason about -- a failure invented by the runtime.
        failed = [
            e for e in agent.store.events(paused.task_id) if e.type is EventType.TOOL_FAILED
        ]
        assert not [e for e in failed if e.payload.get("name") == "plan"]

    async def test_edited_steps_are_the_contract(self, scripted, session_id: str) -> None:
        agent, _ = await scripted([{"content": "done"}])
        paused = await _run(agent, session_id)
        proposed = paused.pending_confirmation.arguments["steps"]
        assert len(proposed) >= 2, "the test needs more than one step to drop one"

        # One step is kept, and it is *not* the first one: dropping the head
        # is the case where "the plan is a contract" is easiest to fake.
        result = await agent.runtime.approve(
            paused.task_id,
            plan=[{"description": "只看第二个问题"}],
        )

        assert result.status == TaskStatus.COMPLETED.value
        state = replay(agent.store.events(paused.task_id), task_id=paused.task_id)
        assert [s.description for s in state.plan] == ["只看第二个问题"]

    async def test_an_empty_plan_is_refused(self, scripted, session_id: str) -> None:
        agent, _ = await scripted([{"content": "done"}])
        paused = await _run(agent, session_id)
        with pytest.raises(ValueError, match="计划不能为空"):
            agent.runtime.approve(paused.task_id, plan=[])

    async def test_rejecting_replans_instead_of_stopping(
        self, scripted, session_id: str
    ) -> None:
        agent, _ = await scripted([{"content": "done"}])
        paused = await _run(agent, session_id)

        result = await agent.runtime.deny(paused.task_id, note="太宽了，只改 README")

        events = agent.store.events(paused.task_id)
        types = [e.type for e in events]
        assert EventType.PLAN_REJECTED in types
        # A second plan was proposed and is waiting again: "no" means "try
        # again", not "stop".
        requested = [e for e in events if e.type is EventType.CONFIRMATION_REQUESTED]
        assert len(requested) == 2
        assert requested[-1].payload["kind"] == "plan"
        assert result.status == TaskStatus.WAITING_CONFIRMATION.value

        state = replay(events, task_id=paused.task_id)
        assert any("rejected the plan" in e.text for e in state.log)
        # A rejected plan is not a permission refusal.
        assert state.denied_streak == 0

    async def test_the_note_reaches_the_agent(self, scripted, session_id: str) -> None:
        agent, model = await scripted([{"content": "done"}])
        paused = await _run(agent, session_id)
        await agent.runtime.deny(paused.task_id, note="先看测试再改代码")

        state = replay(agent.store.events(paused.task_id), task_id=paused.task_id)
        assert any("先看测试再改代码" in e.text for e in state.log)


class TestResumeKeepsTheContract:
    async def test_the_gate_survives_a_resume(self, scripted, session_id: str) -> None:
        """A resume must not silently skip a gate the original run set."""
        agent, _ = await scripted([{"content": "done"}])
        paused = await _run(agent, session_id)

        # Simulate a crash-and-resume before anyone answered.
        again = await agent.runtime.resume(paused.task_id)
        assert again.status == TaskStatus.WAITING_CONFIRMATION.value
        assert again.pending_confirmation.kind == "plan"
