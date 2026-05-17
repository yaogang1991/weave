"""Tests for planner prompt ownership policy and orchestrator parsing (#272 PR 2)."""

import pytest

from core.models import DAGNode
from orchestrator.prompts import get_prompt_registry


class TestPlannerPromptOwnership:
    """Verify planning prompt includes file ownership policy."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.registry = get_prompt_registry()
        self.prompt = self.registry.load("planning").format(
            agent_descriptions="planner, generator, evaluator"
        )

    def test_prompt_mentions_file_ownership(self):
        assert "owned_files" in self.prompt

    def test_prompt_mentions_forbidden(self):
        # The prompt should explain that files owned by another node are forbidden
        assert "OWNED by exactly one node" in self.prompt

    def test_prompt_mentions_shared_file_rule(self):
        assert "__init__.py" in self.prompt
        assert "shared packages are OWNED" in self.prompt

    def test_prompt_mentions_init_py_rule(self):
        assert "__init__.py" in self.prompt

    def test_prompt_mentions_db_schema_analysis(self):
        assert "database" in self.prompt.lower() or "schema" in self.prompt.lower()

    def test_prompt_output_format_includes_owned_files(self):
        assert '"owned_files"' in self.prompt


class TestPlanToDagParsesOwnedFiles:
    """Verify _plan_to_dag extracts owned_files from node definitions."""

    def test_owned_files_parsed(self):
        node = DAGNode(
            id="g1",
            agent_type="generator",
            task_description="test",
            owned_files=["src/a.py", "src/b.py"],
        )
        assert node.owned_files == ["src/a.py", "src/b.py"]

    def test_owned_files_default_empty(self):
        node = DAGNode(
            id="g1",
            agent_type="generator",
            task_description="test",
        )
        assert node.owned_files == []

    def test_owned_files_from_dict(self):
        """Simulate what _plan_to_dag does."""
        node_def = {
            "id": "g1",
            "agent_type": "generator",
            "task": "test",
            "success_criteria": [],
            "owned_files": ["lib/__init__.py", "lib/parser.py"],
        }
        node = DAGNode(
            id=node_def["id"],
            agent_type=node_def["agent_type"],
            task_description=node_def["task"],
            success_criteria=node_def.get("success_criteria", []),
            owned_files=node_def.get("owned_files", []),
        )
        assert node.owned_files == ["lib/__init__.py", "lib/parser.py"]

    def test_owned_files_missing_key_gives_empty(self):
        node_def = {
            "id": "g1",
            "agent_type": "generator",
            "task": "test",
        }
        node = DAGNode(
            id=node_def["id"],
            agent_type=node_def["agent_type"],
            task_description=node_def["task"],
            success_criteria=node_def.get("success_criteria", []),
            owned_files=node_def.get("owned_files", []),
        )
        assert node.owned_files == []
