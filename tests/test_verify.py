"""The project's own check, run before the run may claim success.

Two failure modes this exists for, and both are silent without it:

* the agent writes code that does not run, and reports success because
  nothing in the loop ever executed it;
* the agent *does* run the tests, sees them fail, and answers anyway.

What is tested here is that the check is (a) off unless configured, (b) only
run when something was actually written, and (c) able to refuse a completion
and put the failure back in front of the model.
"""

from __future__ import annotations

from wukong.agent.state import TaskStatus, replay
from wukong.observability.events import EventType


def _configure(settings, command: str, **kwargs) -> None:  # noqa: ANN001
    settings.agent.verify_command = command
    for key, value in kwargs.items():
        setattr(settings.agent, key, value)


async def _write_then_finish(agent, session_id: str):  # noqa: ANN001
    return await agent.runtime.run(
        "write the file", session_id=session_id, model_alias="scripted"
    )


WRITE_STEP = {
    "tool_calls": [
        {"name": "write_file", "arguments": {"path": "out.py", "content": "x = 1\n"}}
    ]
}


class TestWhenItRuns:
    async def test_off_by_default(self, scripted, session_id: str) -> None:
        agent, _ = await scripted([WRITE_STEP, {"content": "done"}])
        result = await _write_then_finish(agent, session_id)

        assert result.status == TaskStatus.COMPLETED.value
        types = [e.type for e in agent.store.events(result.task_id)]
        assert EventType.VERIFY_STARTED not in types

    async def test_a_run_that_wrote_nothing_is_not_verified(
        self, scripted, session_id: str, settings
    ) -> None:  # noqa: ANN001
        _configure(settings, "false")  # would fail if it ever ran
        agent, _ = await scripted([{"content": "just an answer"}])
        result = await _write_then_finish(agent, session_id)

        assert result.status == TaskStatus.COMPLETED.value
        types = [e.type for e in agent.store.events(result.task_id)]
        assert EventType.VERIFY_STARTED not in types

    async def test_a_write_triggers_the_check(self, scripted, session_id: str, settings) -> None:  # noqa: ANN001
        _configure(settings, "true")
        agent, _ = await scripted([WRITE_STEP, {"content": "done"}])
        result = await _write_then_finish(agent, session_id)

        events = agent.store.events(result.task_id)
        finished = [e for e in events if e.type is EventType.VERIFY_FINISHED]
        assert len(finished) == 1
        assert finished[0].payload["passed"] is True
        assert finished[0].payload["command"] == "true"
        assert result.status == TaskStatus.COMPLETED.value

    async def test_the_match_glob_skips_irrelevant_writes(
        self, scripted, session_id: str, settings
    ) -> None:  # noqa: ANN001
        _configure(settings, "false", verify_match="src/**/*.py")
        agent, _ = await scripted([WRITE_STEP, {"content": "done"}])
        result = await _write_then_finish(agent, session_id)

        types = [e.type for e in agent.store.events(result.task_id)]
        assert EventType.VERIFY_STARTED not in types
        assert result.status == TaskStatus.COMPLETED.value

    async def test_a_matching_write_is_verified(self, scripted, session_id: str, settings) -> None:  # noqa: ANN001
        _configure(settings, "true", verify_match="**/out.py")
        agent, _ = await scripted([WRITE_STEP, {"content": "done"}])
        result = await _write_then_finish(agent, session_id)

        types = [e.type for e in agent.store.events(result.task_id)]
        assert EventType.VERIFY_STARTED in types


class TestWhenItFails:
    async def test_the_completion_is_refused(self, scripted, session_id: str, settings) -> None:  # noqa: ANN001
        """The whole point: the first finish does not end the run."""
        _configure(settings, "sh -c 'echo boom; exit 1'", verify_max_rounds=2)
        agent, model = await scripted([WRITE_STEP, {"content": "done"}])

        result = await _write_then_finish(agent, session_id)

        events = agent.store.events(result.task_id)
        rounds = [e for e in events if e.type is EventType.VERIFY_FINISHED]
        # Round one refused the completion, so the model was asked again and
        # round two ran as well.
        assert len(rounds) == 2
        assert rounds[0].payload["passed"] is False
        assert "boom" in rounds[0].payload["output_tail"]
        assert result.status == TaskStatus.COMPLETED.value

    async def test_the_model_is_told_what_failed(self, scripted, session_id: str, settings) -> None:  # noqa: ANN001
        _configure(settings, "sh -c 'echo boom; exit 1'", verify_max_rounds=2)
        agent, _ = await scripted([WRITE_STEP, {"content": "done"}])
        result = await _write_then_finish(agent, session_id)

        state = replay(agent.store.events(result.task_id), task_id=result.task_id)
        notices = [entry.text for entry in state.log if entry.kind == "system"]
        assert any("not finished" in text for text in notices)
        assert any("boom" in text for text in notices)

    async def test_it_stops_after_the_round_limit(self, scripted, session_id: str, settings) -> None:  # noqa: ANN001
        """A model that cannot fix it must not spend the whole budget trying."""
        _configure(settings, "false", verify_max_rounds=1)
        agent, _ = await scripted([WRITE_STEP, {"content": "done"}])
        result = await _write_then_finish(agent, session_id)

        events = agent.store.events(result.task_id)
        assert len([e for e in events if e.type is EventType.VERIFY_FINISHED]) == 1
        state = replay(events, task_id=result.task_id)
        assert any("still fails after 1 attempt" in e.text for e in state.log)

    async def test_a_failed_check_does_not_clear_the_write_list(
        self, scripted, session_id: str, settings
    ) -> None:  # noqa: ANN001
        """Otherwise the second completion would skip the check it just failed."""
        _configure(settings, "false", verify_max_rounds=2)
        agent, _ = await scripted([WRITE_STEP, {"content": "done"}])
        result = await _write_then_finish(agent, session_id)

        state = replay(agent.store.events(result.task_id), task_id=result.task_id)
        assert state.recent_writes, "a failing check leaves the writes pending"
        assert state.verify_rounds == 2

    async def test_a_passing_check_clears_them(self, scripted, session_id: str, settings) -> None:  # noqa: ANN001
        _configure(settings, "true")
        agent, _ = await scripted([WRITE_STEP, {"content": "done"}])
        result = await _write_then_finish(agent, session_id)

        state = replay(agent.store.events(result.task_id), task_id=result.task_id)
        assert state.recent_writes == []


class TestThePermissionStory:
    async def test_it_does_not_go_through_the_permission_engine(
        self, scripted, session_id: str, settings
    ) -> None:  # noqa: ANN001
        """The command is the user's, not the model's.

        `execute_local` is set to DENY here. A model-issued `run_command`
        would be refused; the configured check still runs, because gating it
        behind the model's effect budget would mean a user who wrote
        `verify_command` in their own settings still cannot use it without
        also granting the model shell access.
        """
        from wukong.tools.permissions import Decision
        from wukong.types import EffectClass

        settings.permissions.defaults[EffectClass.EXECUTE_LOCAL] = Decision.DENY
        _configure(settings, "true")
        agent, _ = await scripted([WRITE_STEP, {"content": "done"}])
        result = await _write_then_finish(agent, session_id)

        events = agent.store.events(result.task_id)
        assert EventType.VERIFY_FINISHED in [e.type for e in events]
        assert result.status == TaskStatus.COMPLETED.value

    async def test_it_still_runs_in_the_workspace(
        self, scripted, session_id: str, settings, workspace
    ) -> None:  # noqa: ANN001
        """The sandbox and the cwd are the task's, not the process's."""
        (workspace / "marker.txt").write_text("here\n", encoding="utf-8")
        _configure(settings, "ls marker.txt")
        agent, _ = await scripted([WRITE_STEP, {"content": "done"}])
        result = await _write_then_finish(agent, session_id)

        events = agent.store.events(result.task_id)
        finished = [e for e in events if e.type is EventType.VERIFY_FINISHED][0]
        assert finished.payload["passed"] is True
        assert "marker.txt" in finished.payload["output_tail"]


class TestTheApprovedPath:
    async def test_a_write_a_human_approved_is_still_checked(
        self, scripted, session_id: str, settings
    ) -> None:  # noqa: ANN001
        """The approved call runs on a different code path.

        It executes in `resume`, not through `_run_tool_calls`, so recording
        the write only in the latter meant a run whose sole write was
        approved finished without ever being verified -- which is the exact
        shape of the most common run there is: one file, one approval.
        """
        from wukong.tools.permissions import Decision
        from wukong.types import EffectClass

        settings.permissions.defaults[EffectClass.WRITE_LOCAL] = Decision.CONFIRM
        _configure(settings, "false", verify_max_rounds=1)

        agent, _ = await scripted([WRITE_STEP, {"content": "done"}])
        paused = await _write_then_finish(agent, session_id)
        assert paused.status == TaskStatus.WAITING_CONFIRMATION.value

        result = await agent.runtime.approve(paused.task_id)

        events = agent.store.events(paused.task_id)
        assert EventType.VERIFY_FINISHED in [e.type for e in events]
        assert result.status == TaskStatus.COMPLETED.value
