"""Tests for #335: existing workspace files scanned and passed to planner."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# _scan_existing_files
# ---------------------------------------------------------------------------

def _import_scan():
    """Import _scan_existing_files from control_plane.service."""
    from control_plane.service import _scan_existing_files
    return _scan_existing_files


def test_scan_finds_py_files(tmp_path):
    scan = _import_scan()

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "core.py").write_text("pass")
    (tmp_path / "app" / "utils.py").write_text("pass")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_core.py").write_text("pass")

    results = scan(tmp_path)
    paths = [r["path"] for r in results]
    assert "app/core.py" in paths
    assert "app/utils.py" in paths
    assert "tests/test_core.py" in paths


def test_scan_classifies_types(tmp_path):
    scan = _import_scan()

    (tmp_path / "app.py").write_text("pass")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("pass")
    (tmp_path / "conftest.py").write_text("pass")

    results = scan(tmp_path)
    by_path = {r["path"]: r["type"] for r in results}
    assert by_path.get("app.py") == "source"
    assert by_path.get("tests/test_app.py") == "test"
    assert by_path.get("conftest.py") == "config"


def test_scan_skips_hidden_and_junk_dirs(tmp_path):
    scan = _import_scan()

    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "cached.py").write_text("pass")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "hook.py").write_text("pass")
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "config.py").write_text("pass")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "model.py").write_text("pass")
    # A real source file
    (tmp_path / "real.py").write_text("pass")

    results = scan(tmp_path)
    paths = [r["path"] for r in results]
    assert paths == ["real.py"]


def test_scan_respects_max_files(tmp_path):
    scan = _import_scan()

    for i in range(20):
        (tmp_path / f"mod_{i:02d}.py").write_text("pass")

    results = scan(tmp_path, max_files=5)
    assert len(results) == 5


def test_scan_handles_missing_dir(tmp_path):
    scan = _import_scan()

    nonexistent = tmp_path / "no_such_dir"
    results = scan(nonexistent)
    assert results == []


# ---------------------------------------------------------------------------
# Orchestrator plan() receives existing_files in project_context
# ---------------------------------------------------------------------------

def test_plan_receives_existing_files_in_prompt(tmp_path):
    """Verify the planner prompt includes an 'Existing Workspace Files' section
    when existing_files are present in project_context."""
    from orchestrator.intelligent_orchestrator import IntelligentOrchestrator

    # Minimal mocks
    mock_registry = MagicMock()
    mock_registry.to_prompt_description.return_value = "Agents: planner, generator, evaluator"
    mock_registry.has_agent.return_value = True
    mock_registry.list_agents.return_value = []

    captured_messages = []

    class FakeLLM:
        def call(self, messages, tools=None, max_retries=None):
            captured_messages.append(messages)
            # Return a valid plan JSON
            return {
                "content": json.dumps({
                    "reasoning": "test",
                    "nodes": [
                        {"id": "impl", "agent_type": "generator",
                         "task": "implement"},
                    ],
                    "edges": [],
                }),
            }

    orchestrator = IntelligentOrchestrator.__new__(IntelligentOrchestrator)
    orchestrator.agent_registry = mock_registry
    orchestrator.llm = FakeLLM()
    orchestrator.learning_optimizer = None
    orchestrator.llm_config = MagicMock()
    orchestrator.llm_config.model = "claude-sonnet-4-6"
    orchestrator.skill_registry = None
    orchestrator._prompt_registry = MagicMock()
    orchestrator._prompt_registry.load.return_value = (
        "Template with {agent_descriptions}"
    )

    import asyncio
    project_context = {
        "project_path": "/fake/path",
        "existing_files": [
            {"path": "app/core.py", "type": "source"},
            {"path": "tests/test_core.py", "type": "test"},
        ],
    }
    asyncio.run(
        orchestrator.plan("test requirement", project_context=project_context)
    )

    user_msg = captured_messages[0][1]["content"]
    assert "Existing Workspace Files" in user_msg
    assert "app/core.py" in user_msg
    assert "tests/test_core.py" in user_msg
    assert "reconcile" in user_msg.lower()


def test_plan_no_existing_files_section_when_empty():
    """When project_context has no existing_files, no section is added."""
    from orchestrator.intelligent_orchestrator import IntelligentOrchestrator

    mock_registry = MagicMock()
    mock_registry.to_prompt_description.return_value = "Agents"
    mock_registry.has_agent.return_value = True
    mock_registry.list_agents.return_value = []

    captured_messages = []

    class FakeLLM:
        def call(self, messages, tools=None, max_retries=None):
            captured_messages.append(messages)
            return {
                "content": json.dumps({
                    "reasoning": "test",
                    "nodes": [
                        {"id": "impl", "agent_type": "generator",
                         "task": "implement"},
                    ],
                    "edges": [],
                }),
            }

    orchestrator = IntelligentOrchestrator.__new__(IntelligentOrchestrator)
    orchestrator.agent_registry = mock_registry
    orchestrator.llm = FakeLLM()
    orchestrator.learning_optimizer = None
    orchestrator.llm_config = MagicMock()
    orchestrator.llm_config.model = "claude-sonnet-4-6"
    orchestrator.skill_registry = None
    orchestrator._prompt_registry = MagicMock()
    orchestrator._prompt_registry.load.return_value = "Template {agent_descriptions}"

    import asyncio
    asyncio.run(
        orchestrator.plan("test requirement",
                          project_context={"project_path": "/fake"})
    )

    user_msg = captured_messages[0][1]["content"]
    assert "Existing Workspace Files" not in user_msg


# ---------------------------------------------------------------------------
# Planning prompt includes reconcile rule
# ---------------------------------------------------------------------------

def test_planning_prompt_has_reconcile_rule():
    prompt = Path("orchestrator/prompts/planning.md").read_text()
    assert "existing_files" in prompt
    assert "reconcile" in prompt.lower() or "Reconcile" in prompt
