"""Tests for #1125 / #1136: non-interactive runs promote the claude_code
permission_mode from ``"default"`` to ``"bypassPermissions"``.

Root cause: ``ClaudeCodeRuntimeConfig.from_core_config`` ignored the run's
non-interactive intent, so the Claude CLI was spawned with
``--permission-mode default``. In ``-p`` mode that silently denies every
file write (and with some third-party LLMs drives a thinking-token flood
that hangs the node until ``node_timeout``) — every generator node failed.
"""
from agent.backends.claude_code import ClaudeCodeRuntimeConfig
from core.config import WeaveConfig


def _cc(permission_mode: str = "default"):
    """A core ClaudeCodeConfig with the given permission_mode."""
    return WeaveConfig().claude_code.model_copy(
        update={"permission_mode": permission_mode},
    )


class TestFromCoreConfigNonInteractive:
    def test_non_interactive_promotes_default_to_bypass(self):
        cc = ClaudeCodeRuntimeConfig.from_core_config(
            _cc("default"), non_interactive=True,
        )
        assert cc.permission_mode == "bypassPermissions"

    def test_interactive_keeps_default(self):
        cc = ClaudeCodeRuntimeConfig.from_core_config(
            _cc("default"), non_interactive=False,
        )
        assert cc.permission_mode == "default"

    def test_non_interactive_respects_explicit_plan(self):
        """An explicit non-default mode is honored even when non-interactive."""
        cc = ClaudeCodeRuntimeConfig.from_core_config(
            _cc("plan"), non_interactive=True,
        )
        assert cc.permission_mode == "plan"

    def test_non_interactive_respects_explicit_bypass(self):
        cc = ClaudeCodeRuntimeConfig.from_core_config(
            _cc("bypassPermissions"), non_interactive=True,
        )
        assert cc.permission_mode == "bypassPermissions"

    def test_default_kwarg_backward_compat(self):
        """from_core_config(config) with no kwarg keeps 'default'."""
        cc = ClaudeCodeRuntimeConfig.from_core_config(_cc("default"))
        assert cc.permission_mode == "default"
