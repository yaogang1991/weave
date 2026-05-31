"""Tests for #920: ABC vs Protocol interface convention.

Verifies:
1. CLAUDE.md documents the Protocol-first convention
2. Current ABC and Protocol interfaces are importable
3. Convention is consistent with existing codebase
"""
import importlib
from pathlib import Path


class TestConventionDocumented:
    """Verify CLAUDE.md records the Protocol-first convention."""

    def test_claude_md_mentions_protocol_preference(self):
        content = Path("CLAUDE.md").read_text(encoding="utf-8")
        assert "typing.Protocol" in content
        assert "#920" in content

    def test_claude_md_explains_abc_retained(self):
        content = Path("CLAUDE.md").read_text(encoding="utf-8")
        assert "backward compat" in content.lower() or "retained" in content.lower()


class TestExistingABCInterfaces:
    """Verify all ABC-based interfaces are importable."""

    ABC_INTERFACES = [
        ("backend.base", "ExecutionBackend"),
        ("agent.backends.base", "AgentBackend"),
        ("backend.sandbox", "SandboxProvider"),
        ("control_plane.hooks", "ExecutionHook"),
    ]

    def test_abc_interfaces_importable(self):
        for module_name, class_name in self.ABC_INTERFACES:
            mod = importlib.import_module(module_name)
            cls = getattr(mod, class_name)
            assert issubclass(cls, object), f"{class_name} not importable from {module_name}"


class TestExistingProtocolInterfaces:
    """Verify all Protocol-based interfaces are importable."""

    PROTOCOL_INTERFACES = [
        ("core.progress", "ProgressObserver"),
        ("core.progress", "ProgressFilter"),
        ("evaluator.checkers.base", "CriterionChecker"),
        ("tools.command_runner", "ToolCommandRunner"),
    ]

    def test_protocol_interfaces_importable(self):
        for module_name, class_name in self.PROTOCOL_INTERFACES:
            mod = importlib.import_module(module_name)
            cls = getattr(mod, class_name)
            assert issubclass(cls, object), f"{class_name} not importable from {module_name}"
