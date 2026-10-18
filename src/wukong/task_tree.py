"""Reconstruct the parent→child task tree from the durable projection.

A sub-agent or workflow child is an ordinary task with `parent_task_id` set
(see `storage/store.py` and `db.py`). The projection already carries
everything a tree view needs -- status, steps, tokens, cost -- so this never
folds events. That is deliberate: a tree is a *summary*, exactly what the
projection is for, and folding every descendant's events would rebuild state
for tasks whose detail you only read on demand with `wukong task show`.

See `wukong task tree <id>`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskNode:
    """One node of the tree: a task and the children it spawned."""

    id: str
    goal: str
    status: str
    steps_used: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    children: list["TaskNode"] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    def walk(self, depth: int = 0):
        """Yield ``(depth, node)`` in pre-order, root first."""
        yield depth, self
        for child in self.children:
            yield from child.walk(depth + 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "status": self.status,
            "steps_used": self.steps_used,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": self.cost_usd,
            "error": self.error,
            "children": [c.as_dict() for c in self.children],
        }


@dataclass
class TaskTotals:
    """Budget and status counts over a node and all its descendants."""

    tasks: int
    steps: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    by_status: dict[str, int]

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out


def build_task_tree(store, root_id: str) -> TaskNode | None:
    """Build the subtree rooted at ``root_id``, or ``None`` if it does not exist.

    Guards against cycles with a visited set: a malformed ``parent_task_id``
    that points back into the tree must not turn a read-only walk into an
    infinite loop.
    """
    root = store.get_task(root_id)
    if root is None:
        return None

    seen: set[str] = set()

    def _node(task: dict[str, Any]) -> TaskNode:
        node = TaskNode(
            id=task["id"],
            goal=task.get("goal") or "",
            status=task.get("status") or "pending",
            steps_used=int(task.get("steps_used") or 0),
            tokens_in=int(task.get("tokens_in") or 0),
            tokens_out=int(task.get("tokens_out") or 0),
            cost_usd=float(task.get("cost_usd") or 0.0),
            error=task.get("error"),
        )
        if task["id"] in seen:
            return node  # cycle: report the node, descend no further
        seen.add(task["id"])
        for child in store.list_children(task["id"]):
            node.children.append(_node(child))
        return node

    return _node(root)


def aggregate(node: TaskNode) -> TaskTotals:
    """Sum budget and status counts over ``node`` and all its descendants."""
    steps = 0
    tokens_in = 0
    tokens_out = 0
    cost = 0.0
    by_status: dict[str, int] = {}
    tasks = 0
    for _depth, n in node.walk():
        tasks += 1
        steps += n.steps_used
        tokens_in += n.tokens_in
        tokens_out += n.tokens_out
        cost += n.cost_usd
        by_status[n.status] = by_status.get(n.status, 0) + 1
    return TaskTotals(
        tasks=tasks,
        steps=steps,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        by_status=by_status,
    )


def render_plain(
    node: TaskNode, *, show_budget: bool = True, max_goal: int = 48
) -> str:
    """A textual tree, no markup -- for non-TTY output and tests.

    The root is printed without a connector; children hang below it with the
    usual ``├──`` / ``└──`` lines and ``│   `` continuations.
    """
    lines: list[str] = []

    def _goal() -> str:
        text = (node.goal or "").replace("\n", " ")
        return text if len(text) <= max_goal else text[: max_goal - 1] + "…"

    def _budget() -> str:
        if not show_budget:
            return ""
        return f"  steps={node.steps_used} tokens={node.tokens} ${node.cost_usd:.4f}"

    lines.append(f"[{node.status}] {node.id}  {_goal()}{_budget()}")

    def _rec(child: TaskNode, prefix: str, is_tail: bool) -> None:
        connector = "└── " if is_tail else "├── "
        goal = (child.goal or "").replace("\n", " ")
        if len(goal) > max_goal:
            goal = goal[: max_goal - 1] + "…"
        budget = (
            f"  steps={child.steps_used} tokens={child.tokens} ${child.cost_usd:.4f}"
            if show_budget
            else ""
        )
        lines.append(f"{prefix}{connector}[{child.status}] {child.id}  {goal}{budget}")
        child_prefix = prefix + ("    " if is_tail else "│   ")
        for i, grandchild in enumerate(child.children):
            _rec(grandchild, child_prefix, i == len(child.children) - 1)

    for i, child in enumerate(node.children):
        _rec(child, "", i == len(node.children) - 1)
    return "\n".join(lines)
