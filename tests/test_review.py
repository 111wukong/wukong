"""Reviewing a write before it lands.

The property that matters is round-tripping: accepting every hunk must give
back exactly what the model proposed, and accepting none must give back
exactly what is on disk. Anything in between is a file that no model wrote and
no human typed, so the only defence is that the two endpoints are exact.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wukong.review import apply_accepted, changeset, diff_hunks, parse_unified, revise


def numbered(count: int, *, eol: bool = True) -> str:
    body = "\n".join(f"line {i}" for i in range(1, count + 1))
    return body + ("\n" if eol else "")


class TestParseUnified:
    def test_line_numbers_are_computed_for_both_sides(self) -> None:
        before = "a\nb\nc\n"
        after = "a\nB\nc\n"
        hunks = diff_hunks(before, after)
        assert len(hunks) == 1
        lines = hunks[0]["lines"]
        assert [line["kind"] for line in lines] == ["ctx", "del", "add", "ctx"]
        # The deleted line is old 2; the added line is new 2. Sharing a number
        # across sides is what makes a gutter readable.
        assert [line["old_no"] for line in lines] == [1, 2, None, 3]
        assert [line["new_no"] for line in lines] == [1, None, 2, 3]

    def test_ids_are_stable_and_unique(self) -> None:
        before = numbered(40)
        after = before.replace("line 5", "X").replace("line 30", "Y")
        ids = [hunk["id"] for hunk in diff_hunks(before, after)]
        assert ids == ["h0", "h1"]

    def test_the_preamble_is_not_a_line_of_the_change(self) -> None:
        text = "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
        hunks = parse_unified(text)
        assert len(hunks) == 1
        assert [line["text"] for line in hunks[0]["lines"]] == ["a", "b"]

    def test_a_newline_marker_carries_no_content(self) -> None:
        text = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n"
        lines = parse_unified(text)[0]["lines"]
        assert [line["text"] for line in lines] == ["a", "b"]


class TestApplyAccepted:
    def test_all_accepted_is_the_proposed_text_exactly(self) -> None:
        before = numbered(30)
        after = before.replace("line 5\n", "FIVE\n")
        assert apply_accepted(before, after, None) == after
        ids = [hunk["id"] for hunk in diff_hunks(before, after)]
        assert apply_accepted(before, after, ids) == after

    def test_none_accepted_is_the_file_on_disk_exactly(self) -> None:
        before = numbered(30)
        after = before.replace("line 5\n", "FIVE\n")
        assert apply_accepted(before, after, []) == before

    def test_a_subset_keeps_one_change_and_drops_the_other(self) -> None:
        before = numbered(40)
        after = before.replace("line 5\n", "FIVE\n").replace("line 30\n", "THIRTY\n")
        hunks = diff_hunks(before, after)
        assert len(hunks) == 2

        only_first = apply_accepted(before, after, [hunks[0]["id"]])
        assert "FIVE" in only_first
        assert "THIRTY" not in only_first
        assert "line 30" in only_first

        only_second = apply_accepted(before, after, [hunks[1]["id"]])
        assert "line 5" in only_second
        assert "THIRTY" in only_second

    def test_a_pure_insertion_lands_in_the_right_place(self) -> None:
        before = "one\ntwo\n"
        after = "zero\none\ntwo\n"
        hunks = diff_hunks(before, after)
        assert apply_accepted(before, after, [hunks[0]["id"]]) == after
        assert apply_accepted(before, after, []) == before

    def test_a_file_without_a_trailing_newline_keeps_not_having_one(self) -> None:
        before = "a\nb\nc"
        after = "a\nB\nc"
        assert apply_accepted(before, after, None) == after
        assert apply_accepted(before, after, []) == before
        assert not apply_accepted(before, after, ["h0"]).endswith("\n")

    def test_a_file_with_a_trailing_newline_keeps_it(self) -> None:
        before = "a\nb\nc\n"
        after = "a\nB\nc\n"
        assert apply_accepted(before, after, ["h0"]).endswith("\n")

    def test_an_empty_result_is_empty_not_a_newline(self) -> None:
        assert apply_accepted("x\n", "", []) == "x\n"
        assert apply_accepted("x\n", "", None) == ""


class TestChangeset:
    def test_modifying_an_existing_file_is_reviewable(self, tmp_path: Path) -> None:
        target = tmp_path / "app.py"
        target.write_text(numbered(10), encoding="utf-8")
        result = changeset(
            "write_file",
            {"path": "app.py", "content": numbered(10).replace("line 3", "three")},
            tmp_path,
        )
        assert result is not None
        assert result["reviewable"] is True
        assert result["partial"] is True
        assert result["files"][0]["path"] == "app.py"
        assert result["files"][0]["status"] == "modify"
        assert result["additions"] == 1
        assert result["deletions"] == 1

    def test_creating_a_file_is_not_reviewable_per_hunk(self, tmp_path: Path) -> None:
        result = changeset("write_file", {"path": "new.py", "content": "x = 1\n"}, tmp_path)
        assert result is not None
        assert result["files"][0]["status"] == "create"
        assert result["reviewable"] is False
        assert "reason" in result

    def test_appending_is_not_reviewable_per_hunk(self, tmp_path: Path) -> None:
        (tmp_path / "log.txt").write_text("a\n", encoding="utf-8")
        result = changeset(
            "write_file", {"path": "log.txt", "content": "b\n", "mode": "append"}, tmp_path
        )
        assert result is not None
        assert result["files"][0]["status"] == "append"
        assert result["reviewable"] is False

    def test_apply_patch_is_one_hunk_per_edit(self, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
        result = changeset(
            "apply_patch",
            {
                "path": "app.py",
                "edits": [
                    {"old": "x = 1", "new": "x = 10"},
                    {"old": "y = 2", "new": "y = 20"},
                ],
            },
            tmp_path,
        )
        assert result is not None
        hunks = result["files"][0]["hunks"]
        assert [hunk["id"] for hunk in hunks] == ["e0", "e1"]
        assert result["reviewable"] is False

    def test_a_command_has_no_changeset(self, tmp_path: Path) -> None:
        assert changeset("run_command", {"command": "ls"}, tmp_path) is None

    def test_a_binary_or_unreadable_target_does_not_raise(self, tmp_path: Path) -> None:
        (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00")
        result = changeset("write_file", {"path": "blob.bin", "content": "x"}, tmp_path)
        assert result is not None
        assert result["files"][0]["status"] == "create"

    def test_huge_diffs_are_truncated_and_not_offered_for_slicing(self, tmp_path: Path) -> None:
        (tmp_path / "big.txt").write_text(numbered(2000), encoding="utf-8")
        content = "\n".join(f"other {i}" for i in range(2000)) + "\n"
        result = changeset("write_file", {"path": "big.txt", "content": content}, tmp_path)
        assert result is not None
        assert result["reviewable"] is False
        assert result["reason"]


class TestRevise:
    def test_only_the_accepted_hunks_reach_the_arguments(self, tmp_path: Path) -> None:
        before = numbered(40)
        (tmp_path / "app.py").write_text(before, encoding="utf-8")
        after = before.replace("line 5\n", "FIVE\n").replace("line 30\n", "THIRTY\n")
        hunks = diff_hunks(before, after)

        arguments = {"path": "app.py", "content": after}
        revised = revise(arguments, [hunks[0]["id"]], tmp_path)
        assert "FIVE" in revised["content"]
        assert "THIRTY" not in revised["content"]
        # The original dict is untouched: the caller still holds the proposal.
        assert arguments["content"] == after

    def test_append_cannot_be_sliced(self, tmp_path: Path) -> None:
        (tmp_path / "log.txt").write_text("a\n", encoding="utf-8")
        with pytest.raises(ValueError, match="覆盖写入"):
            revise({"path": "log.txt", "content": "b\n", "mode": "append"}, [], tmp_path)

    def test_a_missing_file_cannot_be_sliced(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="读不到"):
            revise({"path": "gone.py", "content": "x"}, [], tmp_path)


class TestUnknownHunkIds:
    """A narrowed approval with a typo'd id must not become a full write."""

    def test_an_id_that_is_not_in_the_diff_is_refused(self, tmp_path: Path) -> None:
        before = numbered(20)
        (tmp_path / "app.py").write_text(before, encoding="utf-8")
        after = before.replace("line 5\n", "FIVE\n")
        with pytest.raises(ValueError, match="未知的改动块"):
            revise({"path": "app.py", "content": after}, ["h0", "h99"], tmp_path)

    def test_accepting_no_hunks_is_a_legal_decision(self, tmp_path: Path) -> None:
        """Empty is not a typo: it is "I read it and I want none of it"."""
        before = numbered(20)
        (tmp_path / "app.py").write_text(before, encoding="utf-8")
        after = before.replace("line 5\n", "FIVE\n")
        assert revise({"path": "app.py", "content": after}, [], tmp_path)["content"] == before
