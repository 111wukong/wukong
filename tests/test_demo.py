"""The demo that demonstrates the differentiator.

It is the first thing a new user runs, so it is tested like a product rather
than like a fixture: the file on disk must contain exactly the hunks that were
accepted, and nothing else. If this drifts, the flag says more about the demo
than about the runtime.
"""

from __future__ import annotations

import io

from rich.console import Console

from wukong.demo import run_review_demo


def _run(settings, keep):  # noqa: ANN001, ANN202
    out = io.StringIO()
    result = run_review_demo(
        settings, console=Console(file=out), choose=lambda ids: keep(ids)
    )
    return result, out.getvalue()


class TestKeepingSomeHunks:
    def test_one_hunk_lands_and_the_other_does_not(self, settings) -> None:  # noqa: ANN001
        result, _ = _run(settings, lambda ids: ids[:1])

        assert result.accepted == ["h0"]
        assert result.rejected == ["h1"]
        # The kept change is in the file.
        assert "radius must be positive" in result.final_text
        # The rejected one is not, even though it was in the same proposal.
        assert 'return f"r={radius:.2f} area=' not in result.final_text
        assert result.proved

    def test_the_other_hunk_alone(self, settings) -> None:  # noqa: ANN001
        result, _ = _run(settings, lambda ids: ids[1:])

        assert result.accepted == ["h1"]
        assert "radius must be positive" not in result.final_text
        assert 'return f"r={radius:.2f} area={area(radius):.2f}"' in result.final_text
        assert result.proved


class TestTheWholeProposal:
    def test_everything_writes_the_file_the_agent_proposed(self, settings) -> None:  # noqa: ANN001
        result, _ = _run(settings, lambda ids: ids)

        assert result.rejected == []
        assert "radius must be positive" in result.final_text
        assert 'return f"r={radius:.2f} area={area(radius):.2f}"' in result.final_text
        assert result.proved

    def test_nothing_keeps_the_file_untouched(self, settings) -> None:  # noqa: ANN001
        result, _ = _run(settings, lambda ids: [])

        assert result.accepted == []
        assert "radius must be positive" not in result.final_text
        assert 'return f"r={radius}"' in result.final_text
        assert result.proved


class TestWhatItPrints:
    def test_the_decision_is_shown_before_anything_is_written(
        self, settings
    ) -> None:  # noqa: ANN001
        _, output = _run(settings, lambda ids: ids[:1])

        assert "h0" in output and "h1" in output
        assert "磁盘上什么都没有变" in output, "the prompt must say the disk is untouched yet"

    def test_the_outcome_is_checked_hunk_by_hunk(self, settings) -> None:  # noqa: ANN001
        _, output = _run(settings, lambda ids: ids[:1])

        assert "已写入" in output
        assert "不在文件里" in output
        assert "演示失败" not in output

    def test_the_run_ends_in_a_terminal_state(self, settings) -> None:  # noqa: ANN001
        result, _ = _run(settings, lambda ids: [])
        assert result.task_id
        assert result.evidence["h0"] is False
        assert result.evidence["h1"] is False
