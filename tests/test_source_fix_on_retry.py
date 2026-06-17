"""Tests for #288: test generator can fix source code bugs during retry.

Verifies:
1. Generator prompt includes source code fix permission during retry
2. Rule 18 mentions source code fix permission during retry with #288
"""


class TestRetrySourceFixPermission:
    """Verify the generator prompt allows source fixes during retry."""

    def test_generator_prompt_mentions_source_fixes_on_retry(self):
        """Generator prompt should allow editing source files during retry."""
        from agent.prompts import SYSTEM_PROMPTS
        prompt = SYSTEM_PROMPTS["generator"]
        assert "#288" in prompt
        assert "source files" in prompt
        assert "RETRY" in prompt

    def test_rule_18_allows_source_fixes_on_retry(self):
        """Rule 18 should mention source code fix permission during retries."""
        from agent.prompts import SYSTEM_PROMPTS
        prompt = SYSTEM_PROMPTS["generator"]
        # Find the rule 18 section
        rule18_start = prompt.find("18. IMPORT VERIFICATION")
        assert rule18_start > 0, "Rule 18 not found"
        rule18_end = prompt.find("19.", rule18_start)
        rule18 = prompt[rule18_start:rule18_end]
        assert "RETRY" in rule18
        assert "#288" in rule18
        assert "source files" in rule18

    def test_import_verification_rule_exists(self):
        """Rule 18 IMPORT VERIFICATION should exist in the generator prompt."""
        from agent.prompts import SYSTEM_PROMPTS
        prompt = SYSTEM_PROMPTS["generator"]
        assert "18. IMPORT VERIFICATION" in prompt
        assert "import check" in prompt or "import" in prompt
