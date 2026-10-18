"""The console's one piece of real arithmetic, verified through node.

The console is plain ES modules with no JS test runner in this project. Rather
than leave `subtreeTotals` to eyeballing, it lives in a DOM-free module
(`js/tree.js`) and is exercised here by shelling out to node -- which GitHub's
runners ship, so this runs in CI without adding a JS toolchain to the Python
install. When node is absent the test skips: a Python checkout must not require
JS to be green.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CONSOLE_JS = Path(__file__).resolve().parents[1] / "src/wukong/api/console/js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

# root -> {c1 -> g, c2}; `orphan` names a parent that is not in the view.
ROWS = [
    {
        "id": "root",
        "parent_task_id": None,
        "steps_used": 2,
        "tokens_in": 10,
        "tokens_out": 5,
        "cost_usd": 0.001,
    },
    {
        "id": "c1",
        "parent_task_id": "root",
        "steps_used": 4,
        "tokens_in": 120,
        "tokens_out": 60,
        "cost_usd": 0.011,
    },
    {
        "id": "c2",
        "parent_task_id": "root",
        "steps_used": 3,
        "tokens_in": 90,
        "tokens_out": 45,
        "cost_usd": 0.009,
    },
    {
        "id": "g",
        "parent_task_id": "c1",
        "steps_used": 1,
        "tokens_in": 10,
        "tokens_out": 5,
        "cost_usd": 0.001,
    },
    {
        "id": "orphan",
        "parent_task_id": "missing",
        "steps_used": 7,
        "tokens_in": 1,
        "tokens_out": 1,
        "cost_usd": 0.002,
    },
]


def _totals(tmp_path: Path, rows: list[dict]) -> dict[str, dict]:
    """Run `subtreeTotals` in node and return the Map as a plain dict."""
    module = (CONSOLE_JS / "tree.js").as_uri()
    script = f"""
import {{ subtreeTotals }} from {json.dumps(module)};
const totals = subtreeTotals({json.dumps(rows)});
const out = {{}};
for (const [id, value] of totals) out[id] = value;
console.log(JSON.stringify(out));
"""
    harness = tmp_path / "harness.mjs"
    harness.write_text(script, encoding="utf-8")
    try:
        proc = subprocess.run(
            [NODE, str(harness)], capture_output=True, text=True, timeout=30
        )
    except OSError as exc:  # pragma: no cover - node vanished between which and run
        pytest.skip(f"node could not be spawned: {exc}")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_subtree_totals_sum_each_branch(tmp_path: Path) -> None:
    """A parent's number is the whole fan-out, not just its own budget."""
    totals = _totals(tmp_path, ROWS)

    assert totals["root"]["tasks"] == 4
    assert totals["root"]["steps"] == 10
    assert totals["root"]["tokens"] == 345
    assert totals["root"]["cost"] == pytest.approx(0.022)

    assert totals["c1"]["tasks"] == 2
    assert totals["c1"]["steps"] == 5
    assert totals["c1"]["tokens"] == 195

    assert totals["c2"]["tasks"] == 1
    assert totals["c2"]["steps"] == 3


def test_a_child_without_its_parent_counts_as_a_root(tmp_path: Path) -> None:
    """The sidebar's flat list does the same: a child whose parent is not in
    the page is shown at the top level, so its total must not vanish."""
    totals = _totals(tmp_path, ROWS)

    assert totals["orphan"]["tasks"] == 1
    assert totals["orphan"]["steps"] == 7
