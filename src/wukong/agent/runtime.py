"""The agent loop.

Differences from the spec's loop that matter:

* **Budgets are enforced, not reported.** Steps, tokens, cost and wall
  clock are all checked at the top of every iteration. The spec listed them
  under observability; a metric you only record after the fact does not
  stop a runaway loop.
* **Cancellation is a file, not a flag.** A task started in one process can
  be cancelled from another (`wukong task cancel`), so the check has to cross
  a process boundary.
* **In-flight tool calls are resolved explicitly on resume.** See
  `_resolve_unfinished_calls`. This is the failure mode that makes naive
  "resume" dangerous, and it is the reason the ledger is write-ahead.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from wukong.agent.context import ContextBuilder, plan_is_finished
from wukong.agent.execution_identity import ExecutionIdentity, fingerprint, verify
from wukong.agent.executor import FINISH_TOOL, PLAN_TOOL, SKILL_TOOL, ToolRunner
from wukong.agent.planner import Planner, apply_plan_update
from wukong.agent import repetition
from wukong.agent.repetition import call_signature
from wukong.agent.context import require_vision
from wukong.agent.state import (
    WRITE_TOOLS,
    AgentState,
    LogEntry,
    PendingConfirmation,
    TaskStatus,
    _compact_args,
    replay,
)
from wukong.config import Settings
from wukong.errors import ConfirmationRequired, ModelError
from wukong.models.registry import ModelRegistry
from wukong.observability.bus import EventBus
from wukong.observability.events import EventType
from wukong.review import changeset as review_changeset
from wukong.review import revise as review_revise
from wukong.tools.base import ToolContext
from wukong.tools.permissions import PermissionEngine, scrub_env
from wukong.tools.registry import ToolRegistry
from wukong.types import EffectClass, ImageRef, TokenUsage, ToolCall

ProgressHook = Callable[[str, dict[str, Any]], None]

#: Steps on one plan step before the runtime says something about it.
#:
#: Eight is long enough that a genuinely slow step is not interrupted, and
#: short enough that a stuck one is caught while there is still budget left to
#: recover with.
_STALL_AFTER = 8


class AgentResult(BaseModel):
    task_id: str
    session_id: str
    status: str
    answer: str | None = None
    error: str | None = None
    steps: int = 0
    model_calls: int = 0
    usage: TokenUsage = Field(default_factory=TokenUsage)
    pending_confirmation: PendingConfirmation | None = None
    duration_s: float = 0.0
    plan: list[str] = Field(default_factory=list)

    @property
    def needs_approval(self) -> bool:
        return self.status == TaskStatus.WAITING_CONFIRMATION.value


async def _resolved(result: AgentResult) -> AgentResult:
    """Wrap a finished result as a coroutine.

    `approve` is synchronous up to the point where it resumes the loop, so that
    a bad hunk id raises at the call site rather than inside a background task.
    Its callers `await` the return value, so the early-exit path has to hand
    back something awaitable.
    """
    return result


#: Statuses where a loop is driving the task, so the cancellation marker will
#: be read at the next step boundary.
_LIVE_STATUSES = frozenset({TaskStatus.PENDING, TaskStatus.PLANNING, TaskStatus.RUNNING})


def cancel_task(store: Any, settings: Settings, task_id: str) -> str:
    """Cancel a task, and report what actually happened.

    One of `not_found`, `already_terminal`, `cancelled`, `requested`.

    There are two mechanisms and they are not interchangeable, because a
    running loop holds its own copy of the state and never re-reads the event
    log:

    * a task being **driven** can only be stopped by the marker file --
      appending a terminal event would be overwritten by the loop's next
      write;
    * a task that is **paused** has no loop to read the marker, so a marker
      leaves it waiting for ever. It has to be cancelled by recording the
      terminal state directly.

    Only the marker was implemented. A task paused for approval could
    therefore never be cancelled: the API answered `"cancelling"`, and nothing
    happened. That is the state a user is in every time they are looking at an
    approval prompt -- exactly when they are most likely to want out, and the
    console's Cancel button is right there.

    A module-level function rather than a method so the CLI, which has a store
    but no agent, uses the same decision instead of a second copy of it.
    """
    row = store.get_task(task_id)
    if row is None:
        return "not_found"
    status = TaskStatus(row["status"])
    if status.terminal:
        return "already_terminal"

    if status in _LIVE_STATUSES:
        marker = settings.state_dir / f"{task_id}.cancel"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(time.time()), encoding="utf-8")
        return "requested"

    store.append(
        task_id,
        EventType.TASK_CANCELLED,
        {"reason": f"cancelled while {status.value}"},
    )
    # `append` writes the event; it does not touch the `tasks` projection --
    # the loop normally does that itself with `_persist`, and it is not running
    # here. Without this the event log says cancelled and every read path
    # (`get_task`, the console, `wukong task list`) still says the old status,
    # which is how a fix ends up looking like it did nothing.
    state = replay(store.events(task_id), task_id=task_id, session_id=row.get("session_id") or "")
    state.status = TaskStatus.CANCELLED
    state.error = "cancelled"
    state.pending_confirmation = None
    store.save_projection(
        task_id,
        status=state.status.value,
        state=state.snapshot(),
        steps_used=state.steps_used,
        tokens_in=state.usage.prompt_tokens,
        tokens_out=state.usage.completion_tokens,
        cost_usd=state.usage.cost_usd,
        result={"answer": state.answer, "error": state.error},
        pending_confirmation=None,
        error=state.error,
    )
    return "cancelled"


class AgentRuntime:
    def __init__(
        self,
        *,
        settings: Settings,
        store: Any,
        registry: ToolRegistry,
        engine: PermissionEngine,
        models: ModelRegistry,
        skills: Any = None,
        summarizer: Any = None,
        on_progress: ProgressHook | None = None,
        bus: EventBus | None = None,
        memory: Any = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.registry = registry
        self.engine = engine
        self.models = models
        self.skills = skills
        self.on_progress = on_progress
        self.bus = bus
        self.memory = memory
        self._runner = ToolRunner(
            registry=registry,
            engine=engine,
            store=store,
            settings=settings,
            redactor=store.redactor,
        )
        self._context: ContextBuilder | None = None
        self._model: Any = None
        self._summarizer = summarizer

    # -- public API -------------------------------------------------------
    async def run(
        self,
        goal: str,
        *,
        session_id: str,
        model_alias: str | None = None,
        approved_effects: list[EffectClass] | None = None,
        max_steps: int | None = None,
        task_id: str | None = None,
        parent_task_id: str | None = None,
        reflect: bool | None = None,
        prior_context: str = "",
        plan_approval: bool | None = None,
        images: list[ImageRef] | None = None,
    ) -> AgentResult:
        """`task_id` lets a caller subscribe to the event stream *before* the
        task exists. Without it the SSE endpoint races the first events.

        `reflect` overrides `agent.reflect` for this run only. Sub-agents pass
        `False`: a fan-out of five sub-agents would otherwise pay five extra
        extraction calls, and the orchestrator already reflects once over the
        combined result.
        """
        model = self.models.get(model_alias)
        self._model = model
        effects = [e.value for e in (approved_effects or [])]

        # Checked before the task exists, not after the first step. An
        # attachment the model cannot receive is a settings mistake, and the
        # cheapest place to say so is before a single event is written -- the
        # alternative is a task that fails on step 1 having already recorded
        # a plan it was never allowed to act on.
        state_pre = AgentState(
            task_id="pre", session_id=session_id, goal=goal, images=list(images or [])
        )
        require_vision(model, state_pre)
        for ref in images or []:
            if not Path(ref.path).is_file():
                raise FileNotFoundError(f"attachment is gone: {ref.path}")

        budgets = {
            "max_steps": max_steps or self.settings.agent.max_steps,
            "max_tokens": self.settings.agent.max_tokens,
            "max_cost_usd": self.settings.agent.max_cost_usd,
            "started_at": time.time(),
            "deadline": time.time() + self.settings.agent.wall_clock_s,
            "approved_effects": effects,
            "model": model_alias or self.settings.default_model,
            # Recorded so a resume can refuse to operate somewhere else. The
            # session carries it too, but a task that describes itself does
            # not depend on a join to answer "where does this act".
            "workspace": str(self.settings.workspace),
            # Pausing for plan approval is a property of the task, not of the
            # process that started it, so it is recorded next to the other
            # budgets and folded back on resume.
            "require_plan_approval": (
                self.settings.agent.require_plan_approval
                if plan_approval is None
                else bool(plan_approval)
            ),
            # The window this run is working in, recorded here because the
            # model's capabilities are known at this point and asking again
            # later would mean constructing a client to read a constant.
            "max_context_tokens": getattr(model.capabilities, "max_context_tokens", 0),
            # References, never bytes: the files are under home, and a resume
            # must rebuild the same prompt the model was originally asked to
            # look at.
            "images": [ref.model_dump(mode="json") for ref in (images or [])],
        }
        task_id = self.store.create_task(
            session_id=session_id,
            goal=goal,
            budgets=budgets,
            task_id=task_id,
            parent_task_id=parent_task_id,
        )
        state = AgentState(
            task_id=task_id,
            session_id=session_id,
            goal=goal,
            started_at=budgets["started_at"],
            deadline=budgets["deadline"],
            approved_effects=effects,
            prior_context=prior_context,
            require_plan_approval=budgets["require_plan_approval"],
            images=list(images or []),
        )
        self._context = ContextBuilder(
            settings=self.settings,
            model=model,
            tool_catalog=self.registry.prompt_catalog(),
            skills_index=self.skills.index_prompt() if self.skills else "",
            store=self.store,
            summarizer=self._summarizer or self.models.summarizer(),
        )
        result = await self._drive(state, max_steps=max_steps or self.settings.agent.max_steps)
        await self._reflect(state, result, enabled=reflect)
        return result

    async def resume(self, task_id: str, *, max_steps: int | None = None) -> AgentResult:
        started = time.time()
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"unknown task {task_id}")
        events = self.store.events(task_id)
        state = replay(events, task_id=task_id, session_id=task["session_id"])
        if state.status.terminal:
            return self._result(state, duration=0.0)

        # A task acts where it was created, not where the resuming process
        # happens to have been launched. Without this check, `wukong task approve`
        # run from a different directory silently moved every subsequent side
        # effect -- and the path fence would have allowed it, because the fence
        # is built from *this* process's workspace while the model's view of
        # "the project" came from the recorded one. Two answers to the same
        # question, and the wrong one wins.
        recorded = state.workspace
        if recorded and Path(recorded).resolve() != Path(self.settings.workspace).resolve():
            raise ValueError(
                f"task {task_id} was created in {recorded!r} but this process "
                f"is working in {str(self.settings.workspace)!r}. Resuming it "
                "here would move every side effect. Re-run with "
                f"`--workspace {recorded}`."
            )

        model_alias = None
        for event in events:
            if event.type is EventType.TASK_CREATED:
                model_alias = (event.payload.get("budgets") or {}).get("model")
                break
        model = self.models.get(model_alias)
        self._model = model
        self._context = ContextBuilder(
            settings=self.settings,
            model=model,
            tool_catalog=self.registry.prompt_catalog(),
            skills_index=self.skills.index_prompt() if self.skills else "",
            store=self.store,
            summarizer=self._summarizer or self.models.summarizer(),
        )

        self._transition(state, TaskStatus.RUNNING)
        self.store.append(task_id, EventType.TASK_RESUMED, {"last_seq": self.store.last_seq(task_id)})
        self._progress("resumed", {"task_id": task_id, "status": state.status.value})

        # A human decision taken while the task was paused. Both branches
        # re-enter the loop with the *same* call the model already chose --
        # asking the model again would let it silently pick something else.
        if state.approved_pending is not None and state.approved_pending.kind == "plan":
            # There is no call to execute: `PLAN_APPROVED` already installed
            # the steps. Running the "plan" tool here is what produced
            # `unknown tool 'plan'` in the transcript -- a failure invented by
            # the runtime, attributed to the model.
            state.approved_pending = None
            self._progress("approval_granted", {"tool": "plan", "effect": ""})
        elif state.approved_pending is not None:
            pending = state.approved_pending
            state.approved_pending = None
            self._progress("approval_granted", {"tool": pending.tool, "effect": pending.effect})
            # The approval was for a specific thing. If that thing is no longer
            # what would run -- a different binary resolves, or the file was
            # edited while the request sat waiting -- the approval does not
            # apply to it, and executing anyway would be acting on consent that
            # was given for something else.
            drift = self._identity_drift(pending, state)
            if drift:
                return self._fail(state, drift, started)
            # Execute the *same* call the model chose. `approved=True` only
            # affects the scratchpad label -- the call itself is a first
            # execution, not a replay.
            executed = await self._runner.execute(
                ToolCall(id=pending.request_id, name=pending.tool, arguments=pending.arguments),
                state=state,
                ctx=self._tool_context(state),
                approved=True,
            )
            # The approved call runs here, not through `_run_tool_calls`, so
            # the write has to be recorded here too -- otherwise a run whose
            # only write was approved would finish without ever being checked.
            if executed.success:
                self._note_write(state, pending.tool, pending.arguments)
        elif state.denied_pending is not None:
            pending = state.denied_pending
            state.denied_pending = None
            if pending.kind == "plan":
                # "Not this plan" is not a refusal of permission, so it does
                # not touch the refusal streak. It clears the plan and lets
                # the loop plan again, with the human's words in the log.
                note = ""
                for event in reversed(events):
                    if event.type is EventType.CONFIRMATION_DENIED:
                        note = str(event.payload.get("note") or "")
                        break
                self.store.append(
                    task_id,
                    EventType.PLAN_REJECTED,
                    {"request_id": pending.request_id, "note": note},
                )
                state.append_log(
                    LogEntry(
                        index=0,
                        kind="system",
                        text=(
                            "The user rejected the plan"
                            + (f" with this note: {note}" if note else "")
                            + ". Produce a different plan; do not simply repeat the steps "
                            "that were removed."
                        ),
                    )
                )
                self.store.append(
                    task_id, EventType.LOG_APPENDED, {"entry": state.log[-1].model_dump(mode="json")}
                )
                self._persist(state)
                await self._resolve_unfinished_calls(state)
                result = await self._drive(state, max_steps=max_steps or self.settings.agent.max_steps)
                await self._reflect(state, result)
                return result

            limit = self.settings.permissions.confirmations.denial_limit
            remaining = limit - state.denied_streak
            if limit > 0 and remaining <= 0:
                # A run of refusals is a human saying no, and every further
                # attempt costs a model call and another interruption. Stop
                # here and say so, rather than letting the model circle the
                # gate looking for a phrasing that gets through.
                return self._fail(
                    state,
                    f"stopped after {state.denied_streak} consecutive refusals "
                    f"(limit {limit}): the last was {pending.tool} ({pending.effect}). "
                    "Nothing further was attempted. If the task genuinely needs that "
                    "permission, re-run it with the effect pre-approved.",
                    started,
                )
            state.append_log(
                LogEntry(
                    index=0,
                    kind="system",
                    text=(
                        f"The user REFUSED to approve {pending.tool} ({pending.effect}). "
                        "Do not attempt this call or any other route to the same effect. "
                        "Either finish with what you have, or report the blockage. "
                        + (
                            f"This is refusal {state.denied_streak} of {limit}; "
                            "one more and the task stops automatically."
                            if limit > 0 and remaining == 1
                            else ""
                        )
                    ),
                )
            )
            self.store.append(
                task_id, EventType.LOG_APPENDED, {"entry": state.log[-1].model_dump(mode="json")}
            )
            self._persist(state)

        await self._resolve_unfinished_calls(state)
        result = await self._drive(state, max_steps=max_steps or self.settings.agent.max_steps)
        await self._reflect(state, result)
        return result

    def approve(
        self,
        task_id: str,
        *,
        request_id: str | None = None,
        accept: list[str] | None = None,
        plan: list[dict[str, Any]] | None = None,
    ):
        """Approve the pending call, optionally narrowed to some hunks.

        `accept=None` is the ordinary "yes, all of it". A list of hunk ids is
        a narrower yes, and it is applied by rewriting the pending arguments
        *before* the call runs -- which is why the ledger's write-ahead rule
        pays off here: there is no partially applied change to undo, only a
        different string to write.

        The revision is an event, not a local edit. `resume` replays state
        from events, so a revision held only in memory would vanish on a crash
        between approval and execution and the replay would then write the
        hunks the human had rejected.

        Deliberately not `async def`: everything up to the resume is
        synchronous, so a bad hunk id raises at the call site instead of
        disappearing into a background task. The return value is the coroutine
        for the resumed run, which `await` handles exactly as before.
        """
        state, _ = self._load(task_id)
        pending = state.pending_confirmation
        if pending is None:
            return _resolved(self._result(state, duration=0.0))
        if request_id and request_id != pending.request_id:
            raise ValueError(f"request {request_id!r} is not pending; {pending.request_id!r} is")

        if pending.kind == "plan":
            # The human's steps, or the proposed ones when they only pressed
            # "approve". Either way the plan becomes a contract: the loop is
            # measured against it from here on.
            steps = _normalise_plan(plan) if plan is not None else list(
                pending.arguments.get("steps") or []
            )
            if not steps:
                raise ValueError("计划不能为空：至少保留一个步骤")
            self.store.append(
                task_id, EventType.PLAN_APPROVED, {"steps": steps, "edited": plan is not None}
            )
            self.store.append(
                task_id,
                EventType.CONFIRMATION_GRANTED,
                {"request_id": pending.request_id, "kind": "plan", "tool": "plan", "effect": ""},
            )
            return self.resume(task_id)

        accepted_ids: list[str] | None = None
        if accept is not None:
            ctx = self._tool_context(state)
            revised = review_revise(pending.arguments, accept, ctx.workspace)
            tool = self.registry.maybe_get(pending.tool)
            preview = (
                tool.preview(revised, ctx) if tool else pending.preview
            )
            self.store.append(
                task_id,
                EventType.CONFIRMATION_REVISED,
                {
                    "request_id": pending.request_id,
                    "tool": pending.tool,
                    "arguments": revised,
                    "preview": preview,
                    "review": review_changeset(pending.tool, revised, ctx.workspace),
                    "accepted_hunks": list(accept),
                },
            )
            accepted_ids = list(accept)

        self.store.append(
            task_id,
            EventType.CONFIRMATION_GRANTED,
            {
                "request_id": pending.request_id,
                "kind": pending.kind,
                "tool": pending.tool,
                "effect": pending.effect,
                **({"accepted_hunks": accepted_ids} if accepted_ids is not None else {}),
            },
        )
        return self.resume(task_id)

    async def deny(
        self, task_id: str, *, request_id: str | None = None, note: str = ""
    ) -> AgentResult:
        state, _ = self._load(task_id)
        pending = state.pending_confirmation
        if pending is None:
            return self._result(state, duration=0.0)
        if request_id and request_id != pending.request_id:
            raise ValueError(f"request {request_id!r} is not pending; {pending.request_id!r} is")
        self.store.append(
            task_id,
            EventType.CONFIRMATION_DENIED,
            {"request_id": pending.request_id, "tool": pending.tool, "note": note},
        )
        return await self.resume(task_id)

    def cancel(self, task_id: str) -> str:
        """See `cancel_task`. A method so callers holding a runtime need not
        reach for the module."""
        return cancel_task(self.store, self.settings, task_id)

    def is_cancelled(self, task_id: str) -> bool:
        return (self.settings.state_dir / f"{task_id}.cancel").exists()

    # -- loop -------------------------------------------------------------
    async def _drive(self, state: AgentState, *, max_steps: int) -> AgentResult:
        started = time.time()
        model = self._model

        # planning
        if not state.plan:
            self._transition(state, TaskStatus.PLANNING)
            self._progress("planning", {})
            planner = Planner(model=model, settings=self.settings, store=self.store)
            try:
                steps = await planner.plan(state, tool_names=self.registry.names())
            except ModelError as exc:
                return self._fail(state, f"planning failed: {exc}", started)
            state.plan = steps
            state.sync_current_step()
            self.store.append(
                state.task_id,
                EventType.PLAN_CREATED,
                {"steps": [s.model_dump(mode="json") for s in steps]},
            )
            self._progress("plan", {"steps": [s.description for s in steps]})

        if state.require_plan_approval and not state.plan_approved:
            return self._pause_for_plan(state, started)

        self._transition(state, TaskStatus.RUNNING)

        #: How many steps in a row have gone by on the same plan step.
        #:
        #: A step the model never ticks off is a step it never leaves: the
        #: cursor is *derived* from the checklist, so an unmarked step keeps
        #: the run inside itself until the budget dies. One did exactly that
        #: -- 30 steps, 54 tool calls, every one of them logged under
        #: `step_1`, while the plan above it sat untouched from start to
        #: finish. Nothing else about that run looked wrong, which is why the
        #: stall has to be counted explicitly.
        stalled_on_step = 0
        last_step = state.current_step

        while True:
            # --- budget gate -------------------------------------------
            over = self._check_budget(state, max_steps)
            if over:
                self.store.append(
                    state.task_id,
                    EventType.BUDGET_EXCEEDED,
                    {
                        "kind": over[0],
                        "used": over[1],
                        "limit": over[2],
                        "repeated_calls": sum(
                            1 for n in state.call_counts.values() if n > 1
                        ),
                        "redundant_calls": sum(
                            n - 1 for n in state.call_counts.values() if n > 1
                        ),
                    },
                )
                return self._fail(
                    state,
                    f"stopped: {over[0]} budget exhausted ({over[1]} / {over[2]})"
                    + self._budget_diagnosis(state),
                    started,
                )
            if self.is_cancelled(state.task_id):
                self.store.append(state.task_id, EventType.TASK_CANCELLED, {})
                state.status = TaskStatus.CANCELLED
                state.error = "cancelled"
                self._persist(state)
                self._record_skill_runs(state)
                return self._result(state, duration=time.time() - started)

            state.steps_used += 1
            await self._maybe_compact(state)
            memory_block = await self._memory_block(state)
            messages = self._context.build(state, memory_block=memory_block)

            # --- model call --------------------------------------------
            # One id per assistant turn, so the AG-UI encoder can correlate
            # the ephemeral text deltas with the durable MODEL_RESPONSE event.
            message_id = f"msg_{uuid.uuid4().hex[:12]}"
            try:
                resp = await self._call_model(
                    state,
                    messages,
                    stream=self._stream_callback(state.task_id, message_id),
                )
            except ModelError as exc:
                return self._fail(state, f"model call failed: {exc}", started)
            state.model_calls += 1
            state.usage = state.usage + resp.usage
            self.store.append(
                state.task_id,
                EventType.MODEL_RESPONSE,
                {
                    "phase": "step",
                    "message_id": message_id,
                    "model": resp.model,
                    "usage": resp.usage.model_dump(),
                    "tool_calls": [tc.name for tc in resp.tool_calls],
                    "via_text_protocol": resp.via_text_protocol,
                    "content_preview": (resp.content or "")[:300],
                },
            )
            if resp.content:
                self._progress("assistant", {"text": resp.content})

            # --- no tool call: the answer ------------------------------
            if not resp.wants_tools:
                if not (resp.content or "").strip():
                    # An empty response is not an answer. Reporting `completed`
                    # here is how a task that did nothing looks like a task
                    # that succeeded -- and for a reasoning model it is the
                    # common shape of "the output budget ran out before the
                    # model got to its answer".
                    return self._fail(state, _empty_response_reason(resp), started)
                result = await self._try_complete(state, resp.content, started)
                if result is not None:
                    return result
                continue

            # --- execute ------------------------------------------------
            try:
                finished = await self._run_tool_calls(resp.tool_calls, state)
            except ConfirmationRequired as need:
                return self._pause_for_confirmation(state, need, started)
            self._persist(state)
            if finished is not None:
                return finished

            # --- stalled on a step it will not tick off ------------------
            if state.current_step != last_step:
                last_step = state.current_step
                stalled_on_step = 0
            else:
                stalled_on_step += 1
                if stalled_on_step == _STALL_AFTER:
                    self._note_stalled_step(state, stalled_on_step)

    async def _run_tool_calls(
        self, calls: list[ToolCall], state: AgentState
    ) -> AgentResult | None:
        for call in calls:
            if call.name == PLAN_TOOL:
                await self._apply_plan_tool(call, state)
                continue
            if call.name == FINISH_TOOL:
                result = await self._finish_from_tool(call, state)
                if result is not None:
                    return result
                continue

            verdict = self._repeat_verdict(call, state)
            if verdict.blocked:
                self._refuse_repeat(call, state, verdict)
                continue

            entry = await self._runner.execute(
                call,
                state=state,
                ctx=self._tool_context(state),
                annotation=verdict.note if verdict.annotate else "",
            )
            if verdict.annotate:
                self._note_repeat(entry, state, verdict)
            if call.name == SKILL_TOOL and entry.success:
                self._note_skill_loaded(entry, state)
            if entry.success:
                self._note_write(state, call.name, call.arguments)
            self._progress("tool", {"name": call.name, "success": entry.success})
        return None

    def _note_write(self, state: AgentState, name: str, arguments: dict[str, Any]) -> None:
        """Remember that a write landed, for the check that runs before the end.

        Kept in memory *and* folded from events, because the two paths reach
        the same state differently: a live run never replays, and a resumed
        run never executes. Both have to answer "did anything change since the
        last check?".
        """
        if name not in WRITE_TOOLS:
            return
        raw = str(arguments.get("path") or "")
        if not raw:
            return
        workspace = self._tool_context(state).workspace
        path = Path(raw) if Path(raw).is_absolute() else workspace / raw
        state.recent_writes.append(str(path))
        del state.recent_writes[:-20]

    def _repeat_verdict(self, call: ToolCall, state: AgentState):
        """How many times this exact call has been made, and what to do."""
        signature = call_signature(call.name, call.arguments)
        return repetition.check(
            tool=call.name,
            count=state.call_counts.get(signature, 0),
            stop_at=self.settings.agent.repeat_read_limit,
        )

    def _refuse_repeat(self, call: ToolCall, state: AgentState, verdict) -> None:
        """Decline to spend the tokens, and say so in the model's own log.

        Logged as `kind="system"`, which is load-bearing: only `kind="tool"`
        entries increment `call_counts`, so a refused repeat does not raise its
        own count and cannot escalate itself into a permanent block on a call
        the model has genuinely stopped making.
        """
        self.store.append(
            state.task_id,
            EventType.TOOL_REPEATED,
            {
                "name": call.name,
                "arguments": call.arguments,
                "step_id": state.step_id(),
                "count": verdict.count,
                "action": "refused",
            },
        )
        state.append_log(
            LogEntry(
                index=0,
                step_id=state.step_id(),
                kind="system",
                text=f"{call.name}({_compact_args(call.arguments)}) — {verdict.note}",
                success=False,
            )
        )
        self.store.append(
            state.task_id,
            EventType.LOG_APPENDED,
            {"entry": state.log[-1].model_dump(mode="json")},
        )
        self._progress("tool", {"name": call.name, "success": False, "repeated": True})

    def _note_repeat(self, entry: LogEntry, state: AgentState, verdict) -> None:
        """Record that the model repeated itself, for humans and for audits.

        Purely observational: the annotation itself is already inside the
        entry the executor logged, so there is nothing to fold here. This
        event exists so `wukong task events` and `scripts/audit-task.py` can see
        the loop without having to diff observation bodies.
        """
        self.store.append(
            state.task_id,
            EventType.TOOL_REPEATED,
            {
                "name": entry.tool,
                "arguments": entry.arguments,
                "step_id": entry.step_id,
                "count": verdict.count,
                "action": "annotated",
            },
        )

    def _note_stalled_step(self, state: AgentState, steps: int) -> None:
        """Say out loud that the checklist has not moved.

        The step is deliberately *not* advanced here. Ticking it off on the
        model's behalf would be a guess about work nobody verified, and a
        wrong tick is worse than a stall -- it hides the problem rather than
        naming it, and the model would then build on a step that never
        happened. Naming the stall leaves the judgement where it belongs.
        """
        entry = LogEntry(
            index=0,
            kind="system",
            step_id=state.step_id(),
            text=(
                f"You have now spent {steps} steps on this one step without "
                f"ticking it off. The cursor is derived from the plan's marks, "
                f"so it cannot move until you call `update_plan`.\n"
                f"- If the step is done: call `update_plan` now, with it marked "
                f"`completed`.\n"
                f"- If it is not done: stop reading more files. Say what is "
                f"missing and what you will do differently."
            ),
        )
        state.append_log(entry)
        self.store.append(
            state.task_id,
            EventType.LOG_APPENDED,
            {"entry": entry.model_dump(mode="json")},
        )

    def _note_skill_loaded(self, entry: LogEntry, state: AgentState) -> None:
        """Record which skills a task actually pulled in.

        `state.loaded_skills` is what the task-end `skill_runs` row is built
        from, so without this the whole skill-effectiveness signal is empty.
        """
        name = str(entry.arguments.get("name") or "").strip()
        if not name:
            return
        if name not in state.loaded_skills:
            state.loaded_skills.append(name)
        self.store.append(
            state.task_id,
            EventType.SKILL_LOADED,
            {
                "name": name,
                "step_id": entry.step_id,
                "allowed_tools": (entry.arguments or {}).get("allowed_tools"),
            },
        )

    async def _apply_plan_tool(self, call: ToolCall, state: AgentState) -> None:
        try:
            args = self.registry.validate_args(PLAN_TOOL, call.arguments)
        except Exception as exc:  # noqa: BLE001
            state.append_log(
                LogEntry(index=0, kind="system", text=f"update_plan rejected: {exc}")
            )
            self.store.append(
                state.task_id,
                EventType.LOG_APPENDED,
                {"entry": state.log[-1].model_dump(mode="json")},
            )
            return
        apply_plan_update(state, args["steps"])
        self.store.append(
            state.task_id,
            EventType.PLAN_REVISED,
            {
                "steps": [s.model_dump(mode="json") for s in state.plan],
                "current_step": state.current_step,
                "reason": args.get("reason"),
            },
        )
        self._progress("plan_revised", {"steps": [s.description for s in state.plan]})

    async def _finish_from_tool(self, call: ToolCall, state: AgentState) -> AgentResult | None:
        """Returns a result to end the task, or None to keep going.

        A malformed `finish` must not end the task with a bogus answer --
        that is worse than another turn. The validation error goes back into
        the scratchpad and the loop continues.
        """
        try:
            args = self.registry.validate_args(FINISH_TOOL, call.arguments)
        except Exception as exc:  # noqa: BLE001
            state.append_log(
                LogEntry(
                    index=0,
                    kind="system",
                    text=f"finish rejected — {exc}. Fix the arguments or answer in plain text.",
                )
            )
            self.store.append(
                state.task_id,
                EventType.LOG_APPENDED,
                {"entry": state.log[-1].model_dump(mode="json")},
            )
            return None
        answer = args["answer"]
        if args.get("status") == "blocked":
            state.answer = answer
            state.error = "the agent reported it was blocked"
            state.status = TaskStatus.FAILED
            self.store.append(
                state.task_id, EventType.TASK_FAILED, {"error": state.error, "answer": answer}
            )
            self._persist(state)
            return self._result(state, duration=0.0)
        return await self._try_complete(state, answer, time.time())

    def _stream_callback(self, task_id: str, message_id: str):
        """Only stream when someone is listening.

        Without the subscriber check, every run pays the SSE parsing cost in
        the provider adapter for output nobody reads.
        """
        bus = self.bus
        if bus is None or not bus.has_subscribers(task_id):
            return None

        def on_delta(text: str) -> None:
            bus.publish_delta(task_id, message_id=message_id, text=text)

        return on_delta

    def _tool_specs(self, state: AgentState) -> list[dict[str, Any]]:
        """The tool list for this turn.

        Once every plan step is settled, `update_plan` has nothing left to do.
        Leaving it on the list is how a run that had already produced its
        answer kept "ticking steps off" a finished checklist until the step
        budget ran out -- thirteen identical revisions and not one line of
        prose. Withdrawing the tool leaves writing the answer as the only
        move that makes sense.
        """
        specs = self.registry.specs()
        if plan_is_finished(state):
            specs = [
                s for s in specs if (s.get("function") or {}).get("name") != PLAN_TOOL
            ]
        return specs

    async def _call_model(self, state: AgentState, messages: list, *, stream: Any = None) -> Any:
        model = self._model
        attempts = max(1, self.settings.agent.model_retry_attempts)
        last: ModelError | None = None
        specs = self._tool_specs(state)
        self.store.append(
            state.task_id,
            EventType.MODEL_REQUEST,
            {
                "phase": "step",
                "messages": len(messages),
                "attempt": 1,
                "tools": len(specs),
            },
        )
        for attempt in range(attempts):
            try:
                return await model.chat(messages, tools=specs, stream=stream)
            except ModelError as exc:
                last = exc
                self.store.append(
                    state.task_id,
                    EventType.MODEL_ERROR,
                    {
                        "phase": "step",
                        "attempt": attempt + 1,
                        "retryable": exc.retryable,
                        "error": str(exc)[:500],
                    },
                )
                if not exc.retryable or attempt == attempts - 1:
                    raise
                delay = min(2**attempt, 8)
                self.store.append(
                    state.task_id,
                    EventType.MODEL_RETRY,
                    {"attempt": attempt + 1, "delay_s": delay, "error": str(exc)[:500]},
                )
                self._progress("model_retry", {"attempt": attempt + 1, "error": str(exc)})
                await asyncio.sleep(delay)
        raise last or ModelError("model call failed")

    # -- resume semantics -------------------------------------------------
    async def _resolve_unfinished_calls(self, state: AgentState) -> None:
        """Decide what to do about tool calls whose outcome we never learned.

        This is the part naive `resume` implementations get wrong. A row with
        status='started' means the process died between "about to touch the
        world" and "here is what happened". Two cases:

        * tool declares `idempotent=True` -> safe to re-run; do it and hand
          the model the fresh result.
        * tool declares `idempotent=False` (any shell command) -> we do NOT
          know whether it ran. Re-running `git commit` or a deploy would be
          a silent double-execution. Instead the model is told the outcome
          is unknown and must verify the state before continuing.
        """
        unfinished = self.store.unfinished_calls(state.task_id)
        for record in unfinished:
            tool = self.registry.maybe_get(record.name)
            if tool is not None and tool.spec.idempotent:
                self.store.finish_tool_call(record.id, status="retry", error="interrupted")
                text = (
                    f"{record.name}({record.arguments}) was interrupted before it finished. "
                    "It is idempotent, so it is being re-run now."
                )
                state.append_log(LogEntry(index=0, kind="system", text=text))
                self.store.append(
                    state.task_id,
                    EventType.LOG_APPENDED,
                    {"entry": state.log[-1].model_dump(mode="json")},
                )
                await self._runner.execute(
                    ToolCall(id=record.id, name=record.name, arguments=record.arguments),
                    state=state,
                    ctx=self._tool_context(state),
                    attempt=int(record.attempt or 1) + 1,
                    replay_reason="resumed",
                )
                continue

            self.store.finish_tool_call(record.id, status="ambiguous", error="outcome unknown")
            self.store.append(
                state.task_id,
                EventType.TOOL_AMBIGUOUS,
                {"name": record.name, "arguments": record.arguments, "step_id": record.step_id},
            )
            text = (
                f"WARNING: {record.name}({record.arguments}) was in flight when the process "
                "stopped. Its outcome is UNKNOWN — it may have taken effect, or not at all. "
                "Do not assume either way: inspect the current state (files, git status, "
                "process output) before you continue. If it was a side-effecting command, "
                "tell the user it may have run twice."
            )
            state.append_log(LogEntry(index=0, kind="system", text=text))
            self.store.append(
                state.task_id,
                EventType.LOG_APPENDED,
                {"entry": state.log[-1].model_dump(mode="json")},
            )

    # -- helpers ----------------------------------------------------------
    def _tool_context(self, state: AgentState) -> ToolContext:
        effects = {EffectClass(e) for e in state.approved_effects}
        # The task's recorded workspace, not the process's. `resume` refuses a
        # mismatch, so these agree -- reading the task's value here is what
        # makes that agreement explicit rather than incidental.
        workspace = Path(state.workspace) if state.workspace else self.settings.workspace
        return ToolContext(
            task_id=state.task_id,
            session_id=state.session_id,
            step_id=state.step_id(),
            workspace=workspace,
            home=self.settings.home,
            artifact_dir=self.settings.artifact_dir,
            approved_effects=frozenset(effects),
            env=scrub_env(known_secrets=self.settings.secret_values()),
        )

    async def _memory_block(self, state: AgentState) -> str:
        """Recalled memories, hybrid when a provider is configured.

        Searched once per task, not once per step. The query is the goal, and
        the goal does not change -- so every step after the first was asking
        the same question and paying for a hybrid search plus a
        MEMORY_SEARCHED event to get the same answer back. One run did that
        thirty times and matched nothing on any of them.
        """
        if state.memory_block is not None:
            return state.memory_block

        mode = "fts"
        if self.memory is not None:
            try:
                hits = await self.memory.recall(state.goal[:200], limit=5)
                mode = "hybrid" if self.memory.embeddings.available else "fts"
            except Exception:  # noqa: BLE001 - recall must never break a task
                hits = []
        else:
            hits = self.store.search_memories(state.goal[:60], limit=5)
        # Recorded even when nothing matched: "recall ran and found nothing"
        # and "recall never ran" are different, and only the event tells them
        # apart when a task behaves as though it had no context.
        self.store.append(
            state.task_id,
            EventType.MEMORY_SEARCHED,
            {
                "query": state.goal[:200],
                "mode": mode,
                "hits": len(hits),
                "ids": [h.get("id") for h in hits],
            },
        )
        if not hits:
            state.memory_block = ""
            return ""
        lines = ["# Recalled memories", ""]
        lines += [f"- [{h['scope']}] {h['content'][:400]}" for h in hits]
        state.memory_block = "\n".join(lines)
        return state.memory_block

    async def _maybe_compact(self, state: AgentState) -> None:
        if self._context is None:
            return
        if await self._context.maybe_compact(state):
            self._progress("compacted", {"summary": (state.compacted_summary or "")[:200]})

    def _check_budget(self, state: AgentState, max_steps: int) -> tuple[str, Any, Any] | None:
        if state.steps_used >= max_steps:
            return ("steps", state.steps_used, max_steps)
        if state.usage.total_tokens >= self.settings.agent.max_tokens:
            return ("tokens", state.usage.total_tokens, self.settings.agent.max_tokens)
        if (
            self.settings.agent.max_cost_usd > 0
            and state.usage.cost_usd >= self.settings.agent.max_cost_usd
        ):
            return ("cost_usd", round(state.usage.cost_usd, 4), self.settings.agent.max_cost_usd)
        if state.deadline and time.time() > state.deadline:
            return ("wall_clock_s", int(time.time() - state.started_at), int(state.deadline - state.started_at))
        return None

    def _budget_diagnosis(self, state: AgentState) -> str:
        """Say *why* the budget ran out, when the reason is that it looped.

        "steps budget exhausted (60 / 60)" is true and useless: it describes
        the counter, not the behaviour. A run that spent its whole budget
        re-reading four files has a specific, nameable cause, and naming it is
        what turns an opaque failure into something a user can act on --
        either by changing the goal or by fixing the tool the agent could not
        get an answer out of.
        """
        repeats = [(sig, n) for sig, n in state.call_counts.items() if n > 1]
        if not repeats:
            return ""
        redundant = sum(n - 1 for _, n in repeats)
        total = sum(state.call_counts.values())
        worst = max(repeats, key=lambda kv: kv[1])
        # The signature is a hash, so recover the human-readable form from the
        # log rather than printing it.
        name, args = _describe_signature(state, worst[0])
        return (
            f"; {redundant} of {total} tool calls were exact repeats "
            f"({len(repeats)} call(s) made more than once). The most repeated was "
            f"{name}({args}) x{worst[1]}"
        )

    def _identity_drift(self, pending: PendingConfirmation, state: AgentState) -> str | None:
        """Why the approved call no longer matches what would run, or None.

        Only reached for a call that has already been approved, so the message
        has to explain that the *approval* is what no longer applies -- not that
        permission was denied.
        """
        recorded = ExecutionIdentity.from_dict(pending.identity)
        if recorded is None:
            return None
        ctx = self._tool_context(state)
        mismatch = verify(recorded, workspace=ctx.workspace, env=ctx.env)
        if not mismatch:
            return None
        return (
            f"refusing to run the approved {pending.tool}: {mismatch} "
            "The approval was for what was shown at the time, so it does not "
            "cover this. Ask again if you still want it."
        )

    def _pause_for_confirmation(
        self, state: AgentState, need: ConfirmationRequired, started: float
    ) -> AgentResult:
        request_id = f"req_{uuid.uuid4().hex[:10]}"
        tool = self.registry.maybe_get(need.tool)
        preview = (
            tool.preview(need.arguments, self._tool_context(state))
            if tool
            else f"{need.tool}({need.arguments})"
        )
        # Fingerprint what this call will act on, while the human is looking at
        # it. Checked again immediately before it runs -- see
        # `execution_identity` for why an approval gate creates this gap.
        ctx = self._tool_context(state)
        identity = fingerprint(
            need.tool, need.arguments, workspace=ctx.workspace, env=ctx.env
        )
        # Computed once, here, and carried on the event: the reviewer sees it
        # in the same breath as the prompt, and a client that attaches late
        # replays it rather than re-reading files that may have changed since.
        review = review_changeset(need.tool, need.arguments, ctx.workspace)
        pending = PendingConfirmation(
            request_id=request_id,
            step_id=state.step_id(),
            tool=need.tool,
            arguments=need.arguments,
            effect=need.effect,
            reason=str(need),
            preview=preview,
            identity=identity.as_dict() if identity else {},
            review=review,
        )
        state.pending_confirmation = pending
        state.status = TaskStatus.WAITING_CONFIRMATION
        self.store.append(
            state.task_id,
            EventType.CONFIRMATION_REQUESTED,
            {
                "request_id": request_id,
                "step_id": pending.step_id,
                "tool": pending.tool,
                "arguments": pending.arguments,
                "effect": pending.effect,
                "reason": pending.reason,
                "preview": preview,
                "identity": pending.identity,
                "review": review,
            },
        )
        self._persist(state)
        self._progress(
            "confirmation",
            {"preview": preview, "effect": pending.effect, "detail": identity.detail if identity else ""},
        )
        return self._result(state, duration=time.time() - started)

    def _pause_for_plan(self, state: AgentState, started: float) -> AgentResult:
        """Stop after planning, before the first tool call.

        Every other gate in this runtime fires after the money is spent: the
        tool approval fires once the model has already decided what to do,
        and the budget fires once the budget is gone. This is the only one
        that can prevent an expensive mistake instead of reporting it.

        It reuses the confirmation container rather than inventing a second
        pause mechanism, because a plan decision and a tool decision are the
        same shape -- stop, ask, record the answer, resume -- and two
        mechanisms would be two places to get the resume semantics wrong.
        """
        request_id = f"plan_{uuid.uuid4().hex[:10]}"
        steps = [s.model_dump(mode="json") for s in state.plan]
        pending = PendingConfirmation(
            request_id=request_id,
            step_id=state.step_id(),
            kind="plan",
            tool="plan",
            arguments={"steps": steps},
            effect="",
            reason="执行前确认计划：可以删掉步骤、改写描述，或者让 Agent 重新规划。",
            preview="\n".join(
                f"{index + 1}. {step.get('description', '')}" for index, step in enumerate(steps)
            ),
        )
        state.pending_confirmation = pending
        state.status = TaskStatus.WAITING_CONFIRMATION
        self.store.append(
            state.task_id,
            EventType.CONFIRMATION_REQUESTED,
            {
                "request_id": request_id,
                "step_id": pending.step_id,
                "kind": "plan",
                "tool": "plan",
                "arguments": pending.arguments,
                "effect": "",
                "reason": pending.reason,
                "preview": pending.preview,
                "identity": {},
                "review": None,
            },
        )
        self._persist(state)
        self._progress("plan_approval", {"steps": steps})
        return self._result(state, duration=time.time() - started)

    # -- verification -----------------------------------------------------
    async def _try_complete(
        self, state: AgentState, answer: str, started: float
    ) -> AgentResult | None:
        """Complete, unless the project's own check says it does not work.

        This is the only place the runtime argues with the model. Everywhere
        else the model's output is taken at face value and the *tools* are
        what get checked; here the claim is "it is done", and there is a
        command that can settle it.

        Returning None means "not done": the failure is in the log, and the
        loop continues with it. The alternative -- completing anyway and
        mentioning the failure in the answer -- is how an agent reports
        success over a red test suite.
        """
        if not self._verify_due(state):
            return self._complete(state, answer, started)

        passed, detail = await self._run_verify(state)
        if passed:
            return self._complete(state, answer, started)

        if state.verify_rounds >= self.settings.agent.verify_max_rounds:
            # Out of attempts. Completing is the honest end here: the failure
            # is recorded, the answer will not claim otherwise, and looping
            # again would only spend the rest of the budget on the same fix.
            state.append_log(
                LogEntry(
                    index=0,
                    kind="system",
                    text=(
                        f"The verification command still fails after "
                        f"{state.verify_rounds} attempt(s). Stop trying to fix it and "
                        "report what is failing and what you changed."
                    ),
                )
            )
            self.store.append(
                state.task_id,
                EventType.LOG_APPENDED,
                {"entry": state.log[-1].model_dump(mode="json")},
            )
            self._persist(state)
            return self._complete(state, answer, started)

        state.append_log(
            LogEntry(
                index=0,
                kind="system",
                text=(
                    "The task is not finished: the verification command failed "
                    f"(round {state.verify_rounds} of "
                    f"{self.settings.agent.verify_max_rounds}).\n\n{detail}\n\n"
                    "Fix the cause, then finish again. Do not report success while "
                    "this fails, and do not disable or edit the check."
                ),
            )
        )
        self.store.append(
            state.task_id,
            EventType.LOG_APPENDED,
            {"entry": state.log[-1].model_dump(mode="json")},
        )
        self._persist(state)
        return None

    def _verify_due(self, state: AgentState) -> bool:
        """Whether the project's check should run before this completion.

        Off unless a command is configured. Due only when something was
        written that has not been checked yet -- a run that only read files
        has nothing to verify, and a second check over the same tree would
        just cost the user a test run.
        """
        agent = self.settings.agent
        if not agent.verify_command.strip():
            return False
        if state.verify_rounds >= agent.verify_max_rounds:
            return False
        if not state.recent_writes:
            return False
        match = agent.verify_match.strip()
        if not match:
            return True
        return any(_glob_match(path, match) for path in state.recent_writes)

    async def _run_verify(self, state: AgentState) -> tuple[bool, str]:
        """Run the configured command and record what happened.

        Executed through the same `run_command` instance the model would use,
        so the sandbox wrapper, the scrubbed environment and the venv on PATH
        all apply -- but *not* through the permission engine, because the
        model did not ask for this and the command is the user's own. See
        `AgentConfig.verify_command` for why that is the whole argument.
        """
        agent = self.settings.agent
        command = agent.verify_command.strip()
        tool = self.registry.maybe_get("run_command")
        ctx = self._tool_context(state)

        self.store.append(
            state.task_id,
            EventType.VERIFY_STARTED,
            {"command": command, "round": state.verify_rounds + 1},
        )
        self._progress("verify_started", {"command": command})

        if tool is None:  # pragma: no cover - the tool is registered by default
            return True, ""

        result = await tool.run(
            {"command": command, "timeout_s": agent.verify_timeout_s}, ctx
        )
        output = (result.output or result.error or "").strip()
        passed = bool(result.success)
        meta = result.metadata or {}
        tail = _tail(output, 4_000)

        self.store.append(
            state.task_id,
            EventType.VERIFY_FINISHED,
            {
                "command": command,
                "passed": passed,
                "exit_code": meta.get("exit_code"),
                "duration_ms": meta.get("duration_ms"),
                "output_tail": tail,
                "round": state.verify_rounds + 1,
            },
        )
        state.verify_rounds += 1
        if passed:
            state.recent_writes = []
        self._persist(state)
        self._progress(
            "verify_finished",
            {"command": command, "passed": passed, "exit_code": meta.get("exit_code")},
        )

        detail = f"$ {command}\n{tail}" if tail else f"$ {command}\n(no output)"
        return passed, detail

    def _complete(self, state: AgentState, answer: str, started: float) -> AgentResult:
        state.answer = answer
        state.status = TaskStatus.COMPLETED
        self.store.append(
            state.task_id,
            EventType.TASK_COMPLETED,
            {"answer": answer, "steps": state.steps_used, "usage": state.usage.model_dump()},
        )
        self._persist(state)
        self._record_skill_runs(state)
        return self._result(state, duration=max(0.0, time.time() - started))

    def _fail(self, state: AgentState, error: str, started: float) -> AgentResult:
        state.error = error
        state.status = TaskStatus.FAILED
        self.store.append(state.task_id, EventType.TASK_FAILED, {"error": error})
        self._persist(state)
        self._record_skill_runs(state)
        return self._result(state, duration=max(0.0, time.time() - started))

    def _record_skill_runs(self, state: AgentState) -> None:
        """Attribute the task's outcome to every skill it loaded.

        This is the only feedback loop a skill has: without it `deprecate`
        is a decision made on vibes, and `skill_runs` stays an empty table
        that the schema promised would answer "does this skill ever help?".
        """
        if not state.loaded_skills:
            return
        outcome = state.status.value
        for name in state.loaded_skills:
            self.store.record_skill_run(
                skill_name=name, task_id=state.task_id, outcome=outcome
            )

    async def _reflect(
        self, state: AgentState, result: AgentResult, *, enabled: bool | None = None
    ) -> None:
        """Post-task learning: one extra model call, at the very end.

        This lives in the runtime rather than in a front end because it is
        part of the kernel. It used to run only from `wukong run --reflect`, so
        a task started over HTTP never wrote a memory and never produced a
        skill candidate -- the learning path existed and was reachable from
        exactly one of the two entry points.
        """
        if enabled is None:
            enabled = self.settings.agent.reflect
        if not enabled or result.status != TaskStatus.COMPLETED.value:
            return
        from wukong.agent.reflector import Reflector

        try:
            reflector = Reflector(
                model=self._model,
                settings=self.settings,
                store=self.store,
                memory=self.memory,
            )
            if not reflector.worth_running(state):
                return
            await reflector.run_and_persist(state)
        except Exception as exc:  # noqa: BLE001 - learning must never fail a task
            self._progress(
                "reflection_failed", {"error": f"{type(exc).__name__}: {exc}"[:300]}
            )

    def _transition(self, state: AgentState, to: TaskStatus) -> None:
        previous = state.status
        if previous is to:
            return
        state.status = to
        self.store.append(
            state.task_id,
            EventType.STATE_TRANSITION,
            {"from": previous.value, "to": to.value},
        )

    def _persist(self, state: AgentState) -> None:
        self.store.save_projection(
            state.task_id,
            status=state.status.value,
            state=state.snapshot(),
            steps_used=state.steps_used,
            tokens_in=state.usage.prompt_tokens,
            tokens_out=state.usage.completion_tokens,
            cost_usd=state.usage.cost_usd,
            result={"answer": state.answer, "error": state.error},
            pending_confirmation=state.pending_confirmation.model_dump(mode="json")
            if state.pending_confirmation
            else None,
            error=state.error,
        )

    def _result(self, state: AgentState, *, duration: float) -> AgentResult:
        return AgentResult(
            task_id=state.task_id,
            session_id=state.session_id,
            status=state.status.value,
            answer=state.answer,
            error=state.error,
            steps=state.steps_used,
            model_calls=state.model_calls,
            usage=state.usage,
            pending_confirmation=state.pending_confirmation,
            duration_s=round(duration, 3),
            plan=[f"[{s.status.value}] {s.description}" for s in state.plan],
        )

    def _load(self, task_id: str) -> tuple[AgentState, dict[str, Any]]:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"unknown task {task_id}")
        return replay(self.store.events(task_id), task_id=task_id, session_id=task["session_id"]), task

    def _progress(self, kind: str, payload: dict[str, Any]) -> None:
        if self.on_progress:
            try:
                self.on_progress(kind, payload)
            except Exception:  # noqa: BLE001 - UI must never break a task
                pass


def _glob_match(path: str, pattern: str) -> bool:
    """Glob match with the one convenience people expect from `**/`.

    `fnmatch` treats `*` as "anything including /", so `src/**/*.py` and
    `src/*.py` behave the same -- which is fine, and simpler than shipping a
    second matcher. The one thing worth special-casing is a leading `**/`,
    which people write meaning "at any depth, including none": without this,
    `**/*.py` would not match `out.py` at the root and the check would never
    run for the most common case.
    """
    candidate = path.replace(os.sep, "/")
    glob = pattern.replace(os.sep, "/")
    if fnmatch.fnmatch(candidate, glob):
        return True
    if glob.startswith("**/") and fnmatch.fnmatch(candidate, glob[3:]):
        return True
    return False


def _tail(text: str, limit: int) -> str:
    """The end of an output, where a test runner puts the failures.

    Head truncation would keep the banner and drop the assertion -- the one
    part a model needs to fix anything.
    """
    if len(text) <= limit:
        return text
    return "… (earlier output trimmed)\n" + text[-limit:]


def _normalise_plan(steps: list[Any]) -> list[dict[str, Any]]:
    """The steps a human actually kept, in the shape the runtime stores.

    Accepts bare strings as well as objects, because the console sends objects
    but a script should not have to know the internal shape to say "do these
    three things". Empty descriptions are dropped rather than rejected: an
    edited plan with a blank row is a row the user cleared, not an error.
    """
    out: list[dict[str, Any]] = []
    for index, raw in enumerate(steps[:12]):
        if isinstance(raw, str):
            description = raw.strip()
            expected: list[str] = []
        elif isinstance(raw, dict):
            description = str(raw.get("description") or "").strip()
            expected = [str(name) for name in (raw.get("expected_tools") or []) if name]
        else:
            continue
        if not description:
            continue
        out.append(
            {
                "id": f"step_{index + 1}",
                "description": description[:400],
                "expected_tools": expected,
                # A re-numbered plan starts from the beginning: the cursor is
                # derived from the checklist, and inheriting a status from a
                # step that was moved would start the run mid-plan.
                "status": "pending",
                "note": "",
            }
        )
    return out


def _describe_signature(state: AgentState, signature: str) -> tuple[str, str]:
    """Recover the readable form of a call from its hash.

    `call_counts` is keyed on a digest, which is right for folding and wrong
    for reporting. The log still holds the arguments, so look there. Best
    effort by design: compaction can drop the entry that had them, and a
    slightly vaguer failure message is not worth carrying a second copy of
    every call's arguments in state forever.
    """
    for entry in reversed(state.log):
        if entry.tool and call_signature(entry.tool, entry.arguments) == signature:
            return entry.tool, _compact_args(entry.arguments)
    return "an earlier call", "arguments no longer in context"


def _empty_response_reason(resp: Any) -> str:
    """Why an empty response is a failure, in terms the user can act on.

    `finish_reason == "length"` is the case worth naming precisely: reasoning
    models spend output tokens on their reasoning, so a budget that looks
    generous for prose can be exhausted before any content is emitted. The
    message says which setting to raise rather than leaving the user to guess
    why their agent "completed" without doing anything.
    """
    if resp.finish_reason == "length":
        return (
            "the model used its entire output budget before producing an answer "
            "(finish_reason=length). Nothing was accomplished. Raise "
            "`models.<alias>.max_output_tokens`, or reduce the reasoning effort "
            "if the provider exposes one."
        )
    return (
        "the model returned no content and no tool calls "
        f"(finish_reason={resp.finish_reason!r}). Nothing was accomplished, so "
        "this is a failure rather than an empty answer."
    )


__all__ = ["AgentRuntime", "AgentResult"]
