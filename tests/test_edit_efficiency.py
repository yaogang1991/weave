"""Tests for #153: edit tool returns before/after context to reduce re-reads.

Covers:
- Edit tool returns Before/After snippet
- Edit tool returns line number
- Edit tool says "No need to re-read"
- Generator prompt includes trust tool results rule
- Write tool output also mentions trust
"""
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.registry import ToolRegistry


# =============================================================================
# Edit tool enhanced return
# =============================================================================


class TestEditToolReturn:
    """Edit tool returns before/after context so agent trusts the result."""

    def test_edit_returns_before_after(self, tmp_path):
        reg = ToolRegistry(base_cwd=str(tmp_path))
        f = tmp_path / "test.py"
        f.write_text("def foo():\n    return 1\n")

        result = reg.execute("edit", {
            "file_path": "test.py",
            "old_string": "return 1",
            "new_string": "return 2",
        })
        assert result.success
        assert "Before:" in result.output
        assert "After:" in result.output
        assert "return 1" in result.output
        assert "return 2" in result.output

    def test_edit_returns_line_number(self, tmp_path):
        reg = ToolRegistry(base_cwd=str(tmp_path))
        f = tmp_path / "test.py"
        f.write_text("line1\nline2\nline3\n")

        result = reg.execute("edit", {
            "file_path": "test.py",
            "old_string": "line2",
            "new_string": "edited",
        })
        assert result.success
        assert "line 2" in result.output

    def test_edit_says_no_reread(self, tmp_path):
        reg = ToolRegistry(base_cwd=str(tmp_path))
        f = tmp_path / "test.py"
        f.write_text("hello world\n")

        result = reg.execute("edit", {
            "file_path": "test.py",
            "old_string": "hello",
            "new_string": "goodbye",
        })
        assert result.success
        assert "No need to re-read" in result.output

    def test_edit_truncates_long_replacement(self, tmp_path):
        reg = ToolRegistry(base_cwd=str(tmp_path))
        f = tmp_path / "test.py"
        old = "\n".join(f"old line {i}" for i in range(10))
        new = "\n".join(f"new line {i}" for i in range(10))
        f.write_text(old + "\n")

        result = reg.execute("edit", {
            "file_path": "test.py",
            "old_string": old,
            "new_string": new,
        })
        assert result.success
        assert "10 lines total" in result.output

    def test_edit_failure_no_before_after(self, tmp_path):
        reg = ToolRegistry(base_cwd=str(tmp_path))
        f = tmp_path / "test.py"
        f.write_text("hello\n")

        result = reg.execute("edit", {
            "file_path": "test.py",
            "old_string": "not found",
            "new_string": "replacement",
        })
        assert not result.success
        assert "Before:" not in result.output

    def test_multiline_edit_context(self, tmp_path):
        reg = ToolRegistry(base_cwd=str(tmp_path))
        f = tmp_path / "test.py"
        f.write_text("class Foo:\n    def bar(self):\n        pass\n")

        result = reg.execute("edit", {
            "file_path": "test.py",
            "old_string": "    def bar(self):\n        pass",
            "new_string": "    def bar(self):\n        return 42",
        })
        assert result.success
        assert "def bar" in result.output
        assert "return 42" in result.output


# =============================================================================
# Generator prompt trust rule
# =============================================================================


class TestGeneratorPromptTrustRule:
    """Generator prompt includes Rule 15: trust tool results."""

    def test_generator_prompt_has_trust_rule(self):
        from agent.agent_pool import WorkerAgent
        prompt = WorkerAgent.SYSTEM_PROMPTS["generator"]
        assert "TRUST TOOL RESULTS" in prompt
        assert "re-read" in prompt.lower()

    def test_generator_prompt_mentions_prefer_tests(self):
        from agent.agent_pool import WorkerAgent
        prompt = WorkerAgent.SYSTEM_PROMPTS["generator"]
        assert "tests or lint over re-reading" in prompt
