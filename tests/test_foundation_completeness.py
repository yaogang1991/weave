"""Tests for #297: foundation node model completeness rule.

Verifies that the planning prompt includes the rule requiring foundation
nodes to define ALL shared models/schemas/tables needed by downstream
feature nodes.
"""

from __future__ import annotations

from pathlib import Path


def test_planning_prompt_has_foundation_rule():
    """Planning prompt should include foundation completeness rule."""
    prompt = Path("orchestrator/prompts/planning.md").read_text(encoding="utf-8")
    assert "Foundation node completeness" in prompt
    assert "ALL shared definitions" in prompt
    assert "no such table" in prompt


def test_foundation_rule_mentions_database():
    """Rule should specifically mention database models and create_all."""
    prompt = Path("orchestrator/prompts/planning.md").read_text(encoding="utf-8")
    assert "database model" in prompt.lower() or "create_all" in prompt
    assert "Account" in prompt  # specific example from the issue
