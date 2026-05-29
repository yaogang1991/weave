"""Tests for #922: generator prompt warns against from __future__ import annotations
in Pydantic model files.

Verifies:
1. Rule 24 exists in the generator prompt
2. Rule mentions the key failure mode (PydanticUserError)
3. Rule references the issue number (#922)
"""
from agent.prompts import SYSTEM_PROMPTS


class TestPydanticFutureAnnotationsRule:
    """Verify the generator prompt prohibits __future__ annotations in Pydantic."""

    def test_rule_24_exists_in_generator_prompt(self):
        """Rule 24 about Pydantic model imports should exist."""
        prompt = SYSTEM_PROMPTS["generator"]
        assert "24. PYDANTIC MODEL IMPORTS" in prompt

    def test_rule_mentions_future_annotations(self):
        """Rule should mention `from __future__ import annotations`."""
        prompt = SYSTEM_PROMPTS["generator"]
        assert "from __future__ import annotations" in prompt

    def test_rule_mentions_pydantic_error(self):
        """Rule should mention PydanticUserError as the failure mode."""
        prompt = SYSTEM_PROMPTS["generator"]
        assert "PydanticUserError" in prompt

    def test_rule_references_issue(self):
        """Rule should reference the issue number."""
        prompt = SYSTEM_PROMPTS["generator"]
        assert "#922" in prompt

    def test_rule_mentions_explicit_imports(self):
        """Rule should recommend explicit imports as the fix."""
        prompt = SYSTEM_PROMPTS["generator"]
        assert "from datetime import date" in prompt
