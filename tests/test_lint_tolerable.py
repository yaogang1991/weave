"""
Tests for _is_retry_tolerable_lint_issue helper (#154).

Table-driven tests verifying which lint codes are safe to tolerate on retry
and which indicate real functional issues.
"""
import pytest

from core.dag_engine import DAGExecutionEngine


class TestIsRetryTolerableLintIssue:
    """Table-driven tests for lint issue tolerability."""

    # (issue_string, expected_result)
    TOLERABLE_CASES = [
        ("main.py:10:E501 line too long", True),
        ("main.py:5:E303 too many blank lines", True),
        ("main.py:1:W291 trailing whitespace", True),
        ("main.py:3:W293 whitespace before ':'", True),
        ("main.py:2:E203 whitespace before ':'", True),
        ("main.py:1:E302 expected 2 blank lines", True),
        ("main.py:10:F401 'os' imported but unused", True),
        ("main.py:8:F841 local variable 'x' is assigned to but never used", True),
        ("main.py:1:E261 at least two spaces before inline comment", True),
        ("main.py:1:E265 block comment should start with '# '", True),
    ]

    INTOLERABLE_CASES = [
        # E999 is SyntaxError — NEVER tolerable
        ("main.py:1:E999 SyntaxError: invalid syntax", False),
        # Functional errors
        ("reporter/report.py:10:import error", False),
        ("main.py:5:syntax error", False),
        ("main.py:1:NameError: name 'x' is not defined", False),
        # Empty / malformed strings
        ("", False),
        ("random text without code", False),
    ]

    @pytest.mark.parametrize("issue,expected", TOLERABLE_CASES)
    def test_tolerable_issues(self, issue, expected):
        assert DAGExecutionEngine._is_retry_tolerable_lint_issue(issue) == expected, \
            f"Expected {expected} for: {issue}"

    @pytest.mark.parametrize("issue,expected", INTOLERABLE_CASES)
    def test_intolerable_issues(self, issue, expected):
        assert DAGExecutionEngine._is_retry_tolerable_lint_issue(issue) == expected, \
            f"Expected {expected} for: {issue}"

    def test_e999_never_tolerated(self):
        """E999 = SyntaxError, must never be treated as lint-only."""
        assert not DAGExecutionEngine._is_retry_tolerable_lint_issue(
            "parser.py:1:E999 SyntaxError"
        )
