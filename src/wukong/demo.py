"""The first thirty seconds.

`wukong demo` proves the loop runs. That is not the interesting part -- every
agent framework can show you a loop. The interesting part is the gate, and a
gate you have to take on faith is not a gate yet.

So this demo does one thing: an agent proposes a change that is *wrong in one
place and right in another*, you keep the right half, and then the file on disk
is printed with a per-hunk check of whether it landed. No API key, no network,
about twenty seconds.

It runs the real runtime -- real planner, real permission engine, real ledger,
real write. The only thing that is scripted is the model, because the point is
the gate rather than the model's judgement.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from wukong.agent.factory import build_agent
from wukong.config import Settings
from wukong.config import ModelSpec
from wukong.models.mock import MockModel
from wukong.models.registry import ModelRegistry
from wukong.types import EffectClass

BEFORE = '''import math


def area(radius):
    """Circle area."""
    return math.pi * radius * radius


def perimeter(radius):
    """Circumference."""
    return 2 * math.pi * radius


def describe(radius):
    return f"r={radius}"


def main():
    print(area(1))
    print(perimeter(1))
    print(describe(1))


if __name__ == "__main__":
    main()
'''

#: Two changes, deliberately in different places, so a partial accept is a
#: real choice rather than a demonstration of one.
#:
#: h0 -- adds a guard clause. Reasonable, but it changes behaviour on negative
#:       input, and that is exactly the kind of edit a reviewer may not want.
#: h1 -- a formatting change to `describe`. Harmless.
AFTER = BEFORE.replace(
    '    """Circle area."""\n    return math.pi * radius * radius',
    '    """Circle area."""\n    if radius < 0:\n'
    '        raise ValueError("radius must be positive")\n'
    "    return math.pi * radius * radius",
).replace(
    'def describe(radius):\n    return f"r={radius}"',
    'def describe(radius):\n    return f"r={radius:.2f} area={area(radius):.2f}"',
)

GOAL = "给 calc.py 加一个参数校验，并把 describe 的输出格式化一下"

#: The model the demo injects. Deliberately not the machine's default: this run
#: must be offline and deterministic wherever it is executed.
DEMO_ALIAS = "demo"


@dataclass
class DemoResult:
    workspace: Path
    home: Path
    task_id: str
    accepted: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    final_text: str = ""
    #: hunk id -> whether its added lines are present in the file afterwards.
    evidence: dict[str, bool] = field(default_factory=dict)

    @property
    def proved(self) -> bool:
        """Did every accepted hunk land, and every rejected hunk stay out?"""
        return all(self.evidence.get(hunk) is True for hunk in self.accepted) and all(
            self.evidence.get(hunk) is False for hunk in self.rejected
        )


def run_review_demo(
    settings: Settings,
    *,
    console: Console,
    choose: Callable[[list[str]], Iterable[str]] | None = None,
) -> DemoResult:
    """Run the demo. `choose` decides which hunks to keep.

    Split from the printing so a test can drive it without a terminal, and so
    the decision can be scripted in docs/CI without pretending a pipe is a
    person.
    """
    root = Path(tempfile.mkdtemp(prefix="wukong-demo-"))
    workspace = root / "ws"
    home = root / "home"
    workspace.mkdir()
    home.mkdir()
    (workspace / "calc.py").write_text(BEFORE, encoding="utf-8")
    (workspace / "README.md").write_text("# demo\n\n一个用来演示的仓库。\n", encoding="utf-8")

    # The demo is *about* the pause, so the write must ask even if this
    # machine's config happens to pre-approve writes.
    from wukong.tools.permissions import Decision

    settings.permissions.defaults[EffectClass.WRITE_LOCAL] = Decision.CONFIRM

    return _run(settings, workspace=workspace, home=home, console=console, choose=choose)


def _run(
    settings: Settings,
    *,
    workspace: Path,
    home: Path,
    console: Console,
    choose: Callable[[list[str]], Iterable[str]] | None,
) -> DemoResult:
    import asyncio

    settings.home = home
    settings.workspace = workspace

    script = [
        {
            "tool_calls": [
                {
                    "name": "write_file",
                    "arguments": {"path": "calc.py", "content": AFTER},
                }
            ]
        }
    ]

    async def _drive() -> DemoResult:
        # Its own alias on purpose: whatever this machine has configured as
        # the default, the demo is offline and deterministic.
        models = ModelRegistry(settings)
        models.register(
            DEMO_ALIAS, MockModel(ModelSpec(provider="mock", model="demo"), script=script)
        )
        agent = await build_agent(settings=settings, models=models, connect_mcp=False)
        try:
            session_id = agent.store.ensure_session(
                name="demo-review", working_dir=str(workspace), model_alias="mock"
            )
            result = await agent.runtime.run(
                GOAL, session_id=session_id, model_alias=DEMO_ALIAS
            )

            pending = result.pending_confirmation
            if pending is None or not pending.review:
                raise RuntimeError(
                    "the demo expected a paused write; the run did not stop for review"
                )

            hunks = pending.review["files"][0]["hunks"]
            ids = [hunk["id"] for hunk in hunks]
            _print_proposal(console, workspace, pending.preview, hunks)

            kept = list(choose(ids)) if choose is not None else _prompt(console, ids)
            kept = [hunk_id for hunk_id in kept if hunk_id in ids]

            if kept:
                await agent.runtime.approve(result.task_id, accept=kept)
            else:
                await agent.runtime.deny(result.task_id, note="演示：全部拒绝")

            final = (workspace / "calc.py").read_text(encoding="utf-8")
            evidence = {
                hunk["id"]: _landed(final, hunk) for hunk in hunks
            }
            outcome = DemoResult(
                workspace=workspace,
                home=home,
                task_id=result.task_id,
                accepted=kept,
                rejected=[hunk_id for hunk_id in ids if hunk_id not in kept],
                final_text=final,
                evidence=evidence,
            )
            _print_outcome(console, outcome)
            return outcome
        finally:
            agent.close()

    return asyncio.run(_drive())


def _landed(text: str, hunk: dict) -> bool:
    """Whether this hunk's added lines are in the file.

    Checked against the added lines rather than against a hash of the result:
    the question a reader has is "did *this change* get written", and a hash
    answers a question they did not ask.
    """
    added = [line["text"] for line in hunk["lines"] if line["kind"] == "add"]
    if not added:
        return False
    return all(line in text for line in added)


def _print_proposal(console: Console, workspace: Path, preview: str, hunks: list[dict]) -> None:
    console.print()
    console.print(
        f"[dim]临时工作区[/dim] {workspace}\n[dim]目标文件  [/dim] calc.py\n"
    )
    console.print(
        Panel(
            _render_hunks(hunks),
            title=f"[bold]Agent 提议修改 calc.py（{len(hunks)} 处）[/bold]",
            subtitle="[dim]审批之前，磁盘上什么都没有变[/dim]",
            border_style="yellow",
        )
    )


def _render_hunks(hunks: list[dict]) -> Text:
    out = Text()
    for index, hunk in enumerate(hunks):
        if index:
            out.append("\n")
        out.append(f"{hunk['id']}  {hunk['header']}\n", style="cyan")
        for line in hunk["lines"]:
            if line["kind"] == "add":
                out.append(f"  + {line['text']}\n", style="green")
            elif line["kind"] == "del":
                out.append(f"  - {line['text']}\n", style="red")
            else:
                out.append(f"    {line['text']}\n", style="dim")
    return out


def _prompt(console: Console, ids: list[str]) -> list[str]:
    import sys

    console.print()
    console.print(
        "[bold]要写入哪些改动？[/bold] "
        "[dim]回车=全部 · 0=全部拒绝 · 1,2=只接受这些[/dim]"
    )
    if not sys.stdin.isatty():
        # A pipe is not a person. Picking one for them is better than hanging
        # or crashing, and saying so keeps the output honest.
        console.print("[dim]（非交互环境：默认只接受第一块）[/dim]")
        return ids[:1]
    try:
        answer = input("> ").strip()
    except EOFError:
        return ids[:1]
    if not answer:
        return ids
    if answer in {"0", "none", "n"}:
        return []
    chosen: list[str] = []
    for token in answer.replace(" ", ",").split(","):
        if not token:
            continue
        try:
            index = int(token)
        except ValueError:
            continue
        if 1 <= index <= len(ids):
            chosen.append(ids[index - 1])
    return chosen


def _print_outcome(console: Console, result: DemoResult) -> None:
    console.print()
    if result.accepted:
        console.print(
            f"[green]✔[/green] 已写入 {', '.join(result.accepted)}"
            + (
                f"[dim]，{', '.join(result.rejected)} 没有写入[/dim]"
                if result.rejected
                else ""
            )
        )
    else:
        console.print("[yellow]✔[/yellow] 全部拒绝，文件保持原样")

    console.print()
    console.print(
        Panel(
            Text(result.final_text, style="dim"),
            title="[bold]calc.py 现在的内容[/bold]",
            border_style="green" if result.proved else "red",
        )
    )

    console.print("[bold]逐块核对[/bold]")
    for hunk_id, present in result.evidence.items():
        if hunk_id in result.accepted:
            mark = "[green]✔ 已写入[/green]" if present else "[red]✘ 说好写入却没有[/red]"
        else:
            mark = "[green]✔ 不在文件里[/green]" if not present else "[red]✘ 拒绝了却写进去了[/red]"
        console.print(f"  {hunk_id}  {mark}")

    console.print()
    if result.proved:
        console.print(
            "[green]这就是全部区别[/green]：被拒绝的改动不是被撤销的，是从来没有存在过。\n"
            "[dim]台账是写前的 —— 审批之前，待写入的内容还只是一个字符串。[/dim]"
        )
    else:
        console.print("[red]演示失败：文件内容和决定不一致。这是一个 bug。[/red]")

    console.print()
    console.print(
        f"[dim]工作区留给你检查：{result.workspace}\n"
        f"看事件流：wukong task events {result.task_id} --home {result.home}[/dim]"
    )
