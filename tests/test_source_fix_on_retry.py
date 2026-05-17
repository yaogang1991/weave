"""Tests for #288: test generator can fix source code bugs during retry.

Verifies:
1. Retry instruction includes source code fix permission
2. Rule 18 mentions source code fix permission during retry
"""
from pathlib import Path

import pytest


class TestRetrySourceFixPermission:
    """Verify the generator prompt allows source fixes during retry."""

    def test_retry_instruction_mentions_source_fixes(self):
        """INCREMENTAL FIX RULES should include rule 7 about source fixes."""
        prompt = Path("agent/agent_pool.py").read_text() + Path("agent/prompts.py").read_text()
        assert "SOURCE CODE FIXES" in prompt
        assert "#288" in prompt

    def test_rule_18_allows_source_fixes_on_retry(self):
        """Rule 18 should mention source code fix permission during retries."""
        prompt = Path("agent/prompts.py").read_text()
        # Find the rule 18 section
        rule18_start = prompt.find("18. IMPORT VERIFICATION")
        assert rule18_start > 0, "Rule 18 not found"
        rule18_end = prompt.find("19.", rule18_start)
        rule18 = prompt[rule18_start:rule18_end]
        assert "RETRY" in rule18
        assert "#288" in rule18
        assert "edit the\n    source files" in rule18

    def test_retry_instruction_rule_7_exists(self):
        """Rule 7 in INCREMENTAL FIX RULES should mention source code bugs."""
        prompt = Path("agent/agent_pool.py").read_text()
        # Find rule 7 in the retry instruction block
        assert "7. SOURCE CODE FIXES" in prompt
        assert "RuntimeError" in prompt
        assert "AttributeError" in prompt
