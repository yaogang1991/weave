"""Tests for cross-file cascade modification guidance (issue #113)."""
from agent.agent_pool import WorkerAgent


class TestGeneratorRefactoringPrompt:
    def test_prompt_mentions_grep_all_references(self):
        """Generator prompt must instruct finding all references before
        modifying enums/constants."""
        prompt = WorkerAgent.SYSTEM_PROMPTS["generator"]
        assert "grep" in prompt.lower()
        assert "ALL references" in prompt or "all references" in prompt

    def test_prompt_mentions_enum_constant_rules(self):
        """Generator prompt must have rules for enum/constant changes."""
        prompt = WorkerAgent.SYSTEM_PROMPTS["generator"]
        assert "enum" in prompt.lower() or "constant" in prompt.lower()
        assert "reference" in prompt.lower()

    def test_prompt_requires_update_all_sites(self):
        """Generator prompt must require updating all reference sites."""
        prompt = WorkerAgent.SYSTEM_PROMPTS["generator"]
        assert "update ALL reference sites" in prompt or "Update ALL reference" in prompt

    def test_prompt_includes_verification_step(self):
        """Generator prompt must include stale reference verification."""
        prompt = WorkerAgent.SYSTEM_PROMPTS["generator"]
        assert "stale reference" in prompt.lower() or "grep -r" in prompt.lower()
