"""Task tree: reconstruct the parent→child fan-out from the projection.

The interesting assertions are not "does it walk a tree". They are the two
properties that make this view trustworthy:

* it reads the durable projection (status, steps, tokens, cost) and therefore
  agrees with `task show` -- a tree built from a different source would drift
  from the truth, which is exactly the "thinner, eventually-inconsistent view"
  the architecture doc warns against;
* it guards against a malformed `parent_task_id` cycle, because a read-only
  walk that loops is worse than one that stops.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from wukong.config import ModelSpec
from wukong.agent.factory import build_agent
from wukong.models.mock import MockModel
from wukong.storage.store import Store
from wukong.task_tree import aggregate, build_task_tree, render_plain


def _leaf(
    store: Store,
    *,
    goal: str,
    parent: str | None = None,
    status: str = "completed",
    steps: int = 3,
    tokens_in: int = 100,
    tokens_out: int = 50,
    cost: float = 0.01,
) -> str:
    """Create a task and set its projection in one step."""
    tid = store.create_task(session_id="s", goal=goal, parent_task_id=parent)
    store.save_projection(
        tid,
        status=status,
        state={},
        steps_used=steps,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
    )
    return tid


# ---------------------------------------------------------------------------
# store-level: the projection is the single source the tree reads
# ---------------------------------------------------------------------------


class TestBuildTree:
    def test_unknown_root_returns_none(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "tree.db")
        try:
            assert build_task_tree(store, "does_not_exist") is None
        finally:
            store.close()

    def test_it_reconstructs_a_two_level_fan_out(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "tree.db")
        try:
            root = _leaf(store, goal="survey the runtime")
            child_a = _leaf(store, goal="find retry reads", parent=root)
            child_b = _leaf(store, goal="find compaction triggers", parent=root)

            node = build_task_tree(store, root)
            assert node is not None
            assert node.id == root
            assert node.goal == "survey the runtime"
            assert sorted(c.id for c in node.children) == sorted([child_a, child_b])
        finally:
            store.close()

    def test_it_descends_to_grandchildren(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "tree.db")
        try:
            root = _leaf(store, goal="root")
            child = _leaf(store, goal="child", parent=root)
            grand = _leaf(store, goal="grandchild", parent=child)

            node = build_task_tree(store, root)
            assert node is not None
            assert len(node.children) == 1
            assert node.children[0].children[0].id == grand
        finally:
            store.close()

    def test_a_cycle_does_not_loop(self, tmp_path: Path) -> None:
        """A `parent_task_id` that points back into the tree must stop the
        walk, not spin."""
        store = Store(tmp_path / "tree.db")
        try:
            root = store.create_task(session_id="s", goal="root")
            child = store.create_task(session_id="s", goal="child", parent_task_id=root)
            # Malformed: the child claims its own child is the root.
            store.conn.execute(
                "UPDATE tasks SET parent_task_id=? WHERE id=?", (root, child)
            )
            store.conn.commit()

            node = build_task_tree(store, root)
            assert node is not None
            assert len(node.children) == 1
            # No infinite descent: the cycle node has no children recorded.
            assert node.children[0].children == []
        finally:
            store.close()


class TestAggregate:
    def test_subtree_totals_sum_self_and_descendants(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "tree.db")
        try:
            root = _leaf(store, goal="root", steps=2, tokens_in=10, tokens_out=5, cost=0.001)
            _leaf(store, goal="a", parent=root, steps=3, tokens_in=100, tokens_out=50, cost=0.01)
            _leaf(store, goal="b", parent=root, steps=1, tokens_in=20, tokens_out=10, cost=0.002)

            node = build_task_tree(store, root)
            totals = aggregate(node)

            assert totals.tasks == 3
            assert totals.steps == 6
            assert totals.tokens_in == 130
            assert totals.tokens_out == 65
            assert totals.cost_usd == pytest.approx(0.013)
            assert totals.by_status == {"completed": 3}
        finally:
            store.close()

    def test_status_counts_reflect_mixed_outcomes(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "tree.db")
        try:
            root = _leaf(store, goal="root", status="running")
            _leaf(store, goal="a", parent=root, status="completed")
            _leaf(store, goal="b", parent=root, status="failed")

            node = build_task_tree(store, root)
            totals = aggregate(node)
            assert totals.by_status == {"running": 1, "completed": 1, "failed": 1}
        finally:
            store.close()


class TestRender:
    def test_plain_render_shows_ids_and_totals(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "tree.db")
        try:
            root = _leaf(store, goal="survey")
            child = _leaf(store, goal="read the loop", parent=root)
            text = render_plain(build_task_tree(store, root))

            assert root in text
            assert child in text
            assert "survey" in text
            assert "read the loop" in text
            assert "tokens=" in text and "$" in text
            # A proper tree: the child hangs under the root with a connector.
            assert "├──" in text or "└──" in text
        finally:
            store.close()

    def test_render_can_drop_budget(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "tree.db")
        try:
            root = _leaf(store, goal="survey")
            text = render_plain(build_task_tree(store, root), show_budget=False)
            assert "tokens=" not in text
            assert "$" not in text
        finally:
            store.close()


# ---------------------------------------------------------------------------
# real-run path: an actual multi-agent fan-out is readable as a tree
# ---------------------------------------------------------------------------


class _PerAliasModels:
    """A registry handing out a different model per alias (see test_multi_agent).

    `ScriptedModels` shares one instance for every alias, which is fine for a
    single-threaded loop and wrong here: concurrent sub-agents would share one
    script cursor and consume each other's steps.
    """

    def __init__(self, *, orchestrator: Any, worker: Any) -> None:
        self._models = {"scripted": orchestrator, "worker": worker}

    def aliases(self) -> list[str]:
        return sorted(self._models)

    def get(self, alias: str | None = None) -> Any:
        return self._models[alias or "scripted"]

    def try_get(self, alias: str) -> Any:
        return self._models.get(alias)

    def summarizer(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


def _delegate_script(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"tool_calls": [{"name": "delegate", "arguments": {"tasks": tasks}}]},
        {"content": "synthesised"},
    ]


class TestRealFanOut:
    async def test_a_delegation_reads_as_a_tree(self, settings, session_id) -> None:  # noqa: ANN001
        """The whole point of a child task being ordinary: `task tree <root>`
        shows the fan-out and each branch's own budget, with no event replay."""
        from tests.conftest import ScriptedModel

        settings.models["worker"] = ModelSpec(provider="mock", model="mock-react")
        settings.multi_agent.enabled = True
        settings.multi_agent.model = "worker"
        settings.multi_agent.parallelism = 1

        agent = await build_agent(
            settings=settings,
            models=_PerAliasModels(
                orchestrator=ScriptedModel(
                    settings.models["scripted"],
                    _delegate_script(
                        [
                            {
                                "goal": "Read the agent loop.",
                                "output_format": "a summary",
                                "boundaries": "runtime.py only",
                            },
                            {
                                "goal": "Read the permission engine.",
                                "output_format": "a summary",
                                "boundaries": "permissions.py only",
                            },
                        ]
                    ),
                ),
                worker=MockModel(settings.models["worker"]),
            ),
        )
        try:
            result = await agent.runtime.run("survey the runtime", session_id=session_id)
            assert result.status == "completed", result.error

            children = agent.store.list_children(result.task_id)
            assert len(children) == 2

            tree = build_task_tree(agent.store, result.task_id)
            assert tree is not None
            assert tree.id == result.task_id
            assert len(tree.children) == 2
            assert all(c.status == "completed" for c in tree.children)

            totals = aggregate(tree)
            assert totals.tasks == 3
            # Each branch carried its own budget; the parent's is small but real.
            assert totals.steps > 0
            assert totals.tokens > 0
        finally:
            agent.close()
