"""
Tests for delta lint: distinguish new vs existing lint issues (#150).

Verifies that _run_lint uses git diff to filter out pre-existing lint
issues and only reports agent-introduced issues as failures.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from evaluator.engine import EvaluatorEngine, LintIssue, parse_flake8_output


@pytest.fixture
def tmp_store(tmp_path):
    from session.store import SessionStore
    return SessionStore(base_path=str(tmp_path / "events"))


@pytest.fixture
def evaluator(tmp_store):
    return EvaluatorEngine(tmp_store, auto_format_before_eval=True)


# ---------------------------------------------------------------------------
# TestLintIssue
# ---------------------------------------------------------------------------


class TestLintIssue:
    def test_parse_single_line(self):
        output = "src/code.py:10:5: E501 line too long (120 > 100 characters)"
        issues = parse_flake8_output(output)
        assert len(issues) == 1
        assert issues[0].path == "src/code.py"
        assert issues[0].line == 10
        assert issues[0].col == 5
        assert issues[0].code == "E501"
        assert "line too long" in issues[0].message

    def test_parse_multiple_lines(self):
        output = (
            "a.py:1:1: E402 module level import not at top\n"
            "b.py:20:80: E501 line too long\n"
        )
        issues = parse_flake8_output(output)
        assert len(issues) == 2
        assert issues[0].code == "E402"
        assert issues[1].code == "E501"

    def test_parse_empty(self):
        assert parse_flake8_output("") == []
        assert parse_flake8_output("no issues found\n") == []

    def test_parse_windows_path(self):
        output = r"src\code.py:10:5: E501 line too long"
        issues = parse_flake8_output(output)
        assert len(issues) == 1
        assert issues[0].code == "E501"

    def test_frozen(self):
        issue = LintIssue(path="a.py", line=1, col=1, code="E501", message="x")
        with pytest.raises(AttributeError):
            issue.code = "E402"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TestDeltaLint
# ---------------------------------------------------------------------------


class TestDeltaLint:
    @patch("evaluator.runner.subprocess.run")
    def test_existing_lint_not_counted_as_failure(
        self, mock_run, evaluator, tmp_path,
    ):
        """Pre-existing E402 on unchanged lines should not cause failure."""
        (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")

        # autoflake dry-run → noop, flake8 → issues
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=""),  # autoflake dry-run
            MagicMock(  # flake8: existing E402
                returncode=1,
                stdout="code.py:1:1: E402 module level import not at top",
            ),
        ]

        # git diff returns empty (no changed lines for this file)
        with patch("evaluator.runner.get_changed_lines", return_value={}):
            passed, msg = evaluator._run_lint(["code.py"], tmp_path)

        # Without git diff info, all issues are potential failures
        # (fallback behavior — can't distinguish)
        assert not passed

    @patch("evaluator.runner.subprocess.run")
    def test_new_issue_on_changed_line_is_failure(
        self, mock_run, evaluator, tmp_path,
    ):
        """New E501 on an agent-changed line should be a failure."""
        (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=""),  # autoflake dry-run
            MagicMock(returncode=0, stdout=""),  # autopep8
            MagicMock(  # flake8: E501 on line 10
                returncode=1,
                stdout="code.py:10:80: E501 line too long",
            ),
        ]

        # git diff shows line 10 was changed by agent
        with patch(
            "evaluator.runner.get_changed_lines",
            return_value={"code.py": {10}},
        ):
            passed, msg = evaluator._run_lint(["code.py"], tmp_path)

        assert not passed
        assert "1 new issue" in msg
        assert "E501" in msg

    @patch("evaluator.runner.subprocess.run")
    def test_existing_issue_on_unchanged_line_is_ignored(
        self, mock_run, evaluator, tmp_path,
    ):
        """E402 on an unchanged line should be ignored."""
        (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),  # autopep8
            MagicMock(
                returncode=1,
                stdout="code.py:33:1: E402 module level import not at top",
            ),
        ]

        # Line 33 was NOT changed by agent
        with patch(
            "evaluator.runner.get_changed_lines",
            return_value={"code.py": {50, 51}},  # only lines 50-51 changed
        ):
            passed, msg = evaluator._run_lint(["code.py"], tmp_path)

        assert passed
        assert "pre-existing" in msg

    @patch("evaluator.runner.subprocess.run")
    def test_mixed_new_and_existing(
        self, mock_run, evaluator, tmp_path,
    ):
        """Mix of new and existing issues: only new ones cause failure."""
        (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),  # autopep8
            MagicMock(
                returncode=1,
                stdout=(
                    "code.py:33:1: E402 module level import not at top\n"
                    "code.py:589:80: E501 line too long\n"
                ),
            ),
        ]

        # Only line 589 was changed
        with patch(
            "evaluator.runner.get_changed_lines",
            return_value={"code.py": {589}},
        ):
            passed, msg = evaluator._run_lint(["code.py"], tmp_path)

        assert not passed
        assert "1 new issue" in msg
        assert "1 existing ignored" in msg

    @patch("evaluator.runner.subprocess.run")
    def test_no_git_diff_falls_back_to_all_issues(
        self, mock_run, evaluator, tmp_path,
    ):
        """When git diff is unavailable, all issues are treated as failures."""
        (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),  # autopep8
            MagicMock(
                returncode=1,
                stdout="code.py:33:1: E402 module level import not at top",
            ),
        ]

        # get_changed_lines returns empty dict (git not available)
        with patch(
            "evaluator.runner.get_changed_lines",
            return_value={},
        ):
            passed, msg = evaluator._run_lint(["code.py"], tmp_path)

        assert not passed
        assert "delta unavailable" in msg

    @patch("evaluator.runner.subprocess.run")
    def test_lint_clean_still_passes(
        self, mock_run, evaluator, tmp_path,
    ):
        """No lint issues → pass regardless of git diff."""
        (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=""),  # autopep8
            MagicMock(returncode=0, stdout=""),
        ]

        passed, msg = evaluator._run_lint(["code.py"], tmp_path)
        assert passed
        assert "clean" in msg.lower()
