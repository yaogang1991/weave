"""Comprehensive tests for orchestrator/planner.py.

Tests the Planner class and module-level helpers:
- __init__ and attribute setup
- plan() method with structured output and free-text fallback
- _plan_structured_output() via tool_use
- _plan_free_text() with retry and truncation handling
- _plan_to_dag() static conversion
- _validate_agents() unregistered agent detection
- _estimate_dag_tokens() token estimation pipeline
- plan_from_template() template-based DAG generation
- _infer_fallback_edges() edge inference from agent types
- _apply_rename_map() criterion and task description renaming
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.dag_models import (
    DAG,
    DAGEdge,
    DAGNode,
    DAGOutputModel,
    DAGNodeModel,
    DependencyType,
    OrchestratorPlan,
)
from core.eval_models import CriterionType, SuccessCriterion
from orchestrator.planner import (
    Planner,
    _apply_rename_map,
    _infer_fallback_edges,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_llm():
    """Mock LLMClient."""
    return MagicMock()


@pytest.fixture
def mock_llm_config():
    """Minimal LLM config with provider and model attributes."""
    cfg = MagicMock()
    cfg.provider = "anthropic"
    cfg.model = "test-model"
    return cfg


@pytest.fixture
def agent_registry():
    """AgentRegistry with default planner/generator/evaluator agents."""
    from core.agent_registry import AgentRegistry
    return AgentRegistry()


@pytest.fixture
def prompt_registry():
    """PromptRegistry pointing at real prompts directory."""
    from orchestrator.prompts import PromptRegistry
    return PromptRegistry()


@pytest.fixture
def planner(mock_llm, mock_llm_config, agent_registry, prompt_registry):
    """Planner instance with mocked LLM and real registries."""
    return Planner(
        llm=mock_llm,
        llm_config=mock_llm_config,
        agent_registry=agent_registry,
        prompt_registry=prompt_registry,
    )


def _make_plan_data(
    nodes=None,
    edges=None,
    reasoning="test reasoning",
):
    """Helper to build a minimal valid plan dict for OrchestratorPlan."""
    if nodes is None:
        nodes = [
            {
                "id": "plan_1",
                "agent_type": "planner",
                "task_description": "Analyze requirements",
                "dependencies": [],
            },
            {
                "id": "gen_1",
                "agent_type": "generator",
                "task_description": "Implement feature",
                "dependencies": ["plan_1"],
            },
            {
                "id": "eval_1",
                "agent_type": "evaluator",
                "task_description": "Evaluate output",
                "dependencies": ["gen_1"],
            },
        ]
    if edges is None:
        edges = [
            {"from": "plan_1", "to": "gen_1"},
            {"from": "gen_1", "to": "eval_1"},
        ]
    return {
        "nodes": nodes,
        "edges": edges,
        "reasoning": reasoning,
    }


def _structured_tool_response(plan_data):
    """Build an LLM response simulating structured tool_use output."""
    dag_model = DAGOutputModel(
        nodes=[
            DAGNodeModel(
                id=n["id"],
                agent_type=n["agent_type"],
                task_description=n.get("task_description", n.get("task", "")),
                dependencies=n.get("dependencies", []),
            )
            for n in plan_data["nodes"]
        ],
        reasoning=plan_data.get("reasoning", ""),
    )
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "name": "generate_dag",
                "arguments": dag_model.model_dump(),
            }
        ],
    }


def _free_text_response(plan_data):
    """Build an LLM response simulating free-text JSON output."""
    return {
        "role": "assistant",
        "content": f"```json\n{json.dumps(plan_data)}\n```",
    }


# ===================================================================
# 1. Planner.__init__ tests
# ===================================================================

class TestPlannerInit:
    def test_init_stores_attributes(self, mock_llm, mock_llm_config, agent_registry, prompt_registry):
        p = Planner(
            llm=mock_llm,
            llm_config=mock_llm_config,
            agent_registry=agent_registry,
            prompt_registry=prompt_registry,
        )
        assert p.llm is mock_llm
        assert p.llm_config is mock_llm_config
        assert p.agent_registry is agent_registry
        assert p._prompt_registry is prompt_registry

    def test_init_defaults_none(self, mock_llm, mock_llm_config, agent_registry, prompt_registry):
        p = Planner(
            llm=mock_llm,
            llm_config=mock_llm_config,
            agent_registry=agent_registry,
            prompt_registry=prompt_registry,
        )
        assert p.learning_optimizer is None
        assert p.skill_registry is None
        assert p._token_estimator is None

    def test_init_optional_dependencies(self, mock_llm, mock_llm_config, agent_registry, prompt_registry):
        optimizer = MagicMock()
        skills = MagicMock()
        estimator = MagicMock()
        p = Planner(
            llm=mock_llm,
            llm_config=mock_llm_config,
            agent_registry=agent_registry,
            prompt_registry=prompt_registry,
            learning_optimizer=optimizer,
            skill_registry=skills,
            token_estimator=estimator,
        )
        assert p.learning_optimizer is optimizer
        assert p.skill_registry is skills
        assert p._token_estimator is estimator


# ===================================================================
# 2. _plan_structured_output tests
# ===================================================================

class TestPlanStructuredOutput:
    def test_structured_output_success(self, planner, mock_llm):
        plan_data = _make_plan_data()
        mock_llm.call.return_value = _structured_tool_response(plan_data)

        result = planner._plan_structured_output([
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "req"},
        ])
        assert result is not None
        assert "nodes" in result
        assert len(result["nodes"]) == 3
        assert result["reasoning"] == "test reasoning"

    def test_structured_output_returns_none_when_no_tool_calls(self, planner, mock_llm):
        mock_llm.call.return_value = {
            "role": "assistant",
            "content": "Here is the plan...",
            "tool_calls": [],
        }
        result = planner._plan_structured_output([
            {"role": "user", "content": "req"},
        ])
        assert result is None

    def test_structured_output_returns_none_on_wrong_tool_name(self, planner, mock_llm):
        mock_llm.call.return_value = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"name": "other_tool", "arguments": {}}],
        }
        result = planner._plan_structured_output([
            {"role": "user", "content": "req"},
        ])
        assert result is None

    def test_structured_output_returns_none_on_empty_arguments(self, planner, mock_llm):
        mock_llm.call.return_value = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"name": "generate_dag", "arguments": None}],
        }
        result = planner._plan_structured_output([
            {"role": "user", "content": "req"},
        ])
        assert result is None

    def test_structured_output_returns_none_on_exception(self, planner, mock_llm):
        mock_llm.call.side_effect = RuntimeError("API error")
        result = planner._plan_structured_output([
            {"role": "user", "content": "req"},
        ])
        assert result is None

    def test_structured_output_prunes_messages(self, planner, mock_llm):
        plan_data = _make_plan_data()
        mock_llm.call.return_value = _structured_tool_response(plan_data)

        # Pass messages that will be pruned
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "req"},
        ]
        planner._plan_structured_output(messages)
        # Verify LLM was called (messages were passed through pruning)
        mock_llm.call.assert_called_once()


# ===================================================================
# 3. _plan_free_text tests
# ===================================================================

class TestPlanFreeText:
    def test_free_text_success_first_attempt(self, planner, mock_llm):
        plan_data = _make_plan_data()
        mock_llm.call.return_value = _free_text_response(plan_data)

        result = planner._plan_free_text([
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "req"},
        ])
        assert result is not None
        assert "nodes" in result
        assert len(result["nodes"]) == 3

    def test_free_text_raises_after_retries(self, planner, mock_llm):
        mock_llm.call.return_value = {
            "role": "assistant",
            "content": "Not JSON at all, just plain text.",
        }
        with pytest.raises(ValueError, match="Failed to parse planning response"):
            planner._plan_free_text([
                {"role": "user", "content": "req"},
            ])

    def test_free_text_retries_on_bad_json(self, planner, mock_llm):
        plan_data = _make_plan_data()
        # First call: invalid JSON, second call: valid JSON
        mock_llm.call.side_effect = [
            {"role": "assistant", "content": "bad response"},
            _free_text_response(plan_data),
        ]
        result = planner._plan_free_text([
            {"role": "user", "content": "req"},
        ])
        assert result is not None
        assert mock_llm.call.call_count == 2

    def test_free_text_sends_truncation_prompt_on_truncated_response(self, planner, mock_llm):
        plan_data = _make_plan_data()
        truncated_content = '{"nodes": [{"id": "a", "agent_type": "generator", "task'  # incomplete
        mock_llm.call.side_effect = [
            {"role": "assistant", "content": truncated_content},
            _free_text_response(plan_data),
        ]
        # Patch is_response_truncated to detect truncation
        with patch("orchestrator.planner.is_response_truncated", return_value=True):
            result = planner._plan_free_text([
                {"role": "user", "content": "req"},
            ])
        assert result is not None


# ===================================================================
# 4. _plan_to_dag tests (static method)
# ===================================================================

class TestPlanToDag:
    def test_converts_plan_to_dag(self):
        plan = OrchestratorPlan(
            reasoning="test",
            nodes=[
                {"id": "n1", "agent_type": "planner", "task_description": "Plan it"},
                {"id": "n2", "agent_type": "generator", "task_description": "Build it",
                 "dependencies": ["n1"]},
                {"id": "n3", "agent_type": "evaluator", "task_description": "Test it",
                 "dependencies": ["n2"]},
            ],
            edges=[
                {"from": "n1", "to": "n2"},
                {"from": "n2", "to": "n3"},
            ],
        )
        dag = Planner._plan_to_dag(plan)
        assert len(dag.nodes) == 3
        assert "n1" in dag.nodes
        assert dag.nodes["n1"].task_description == "Plan it"
        assert dag.nodes["n2"].agent_type == "generator"
        assert len(dag.edges) == 2

    def test_soft_dependency_type(self):
        plan = OrchestratorPlan(
            reasoning="soft deps",
            nodes=[
                {"id": "a", "agent_type": "generator", "task_description": "A"},
                {"id": "b", "agent_type": "evaluator", "task_description": "B"},
            ],
            edges=[
                {"from": "a", "to": "b", "dependency_type": "soft"},
            ],
        )
        dag = Planner._plan_to_dag(plan)
        assert dag.edges[0].dependency_type == DependencyType.SOFT

    def test_empty_edges_triggers_fallback(self):
        plan = OrchestratorPlan(
            reasoning="no edges",
            nodes=[
                {"id": "p1", "agent_type": "planner", "task_description": "Plan"},
                {"id": "g1", "agent_type": "generator", "task_description": "Gen"},
                {"id": "e1", "agent_type": "evaluator", "task_description": "Eval"},
            ],
            edges=[],
        )
        dag = Planner._plan_to_dag(plan)
        # _infer_fallback_edges should have added edges
        assert len(dag.edges) > 0

    def test_task_description_fallback_to_task_key(self):
        plan = OrchestratorPlan(
            reasoning="",
            nodes=[
                {"id": "n1", "agent_type": "planner", "task": "Use task key"},
            ],
            edges=[],
        )
        dag = Planner._plan_to_dag(plan)
        assert dag.nodes["n1"].task_description == "Use task key"

    def test_task_description_fallback_to_description_key(self):
        plan = OrchestratorPlan(
            reasoning="",
            nodes=[
                {"id": "n1", "agent_type": "generator", "description": "Use description key"},
            ],
            edges=[],
        )
        dag = Planner._plan_to_dag(plan)
        assert dag.nodes["n1"].task_description == "Use description key"

    def test_preserves_owned_files_and_backend(self):
        plan = OrchestratorPlan(
            reasoning="",
            nodes=[
                {
                    "id": "n1",
                    "agent_type": "generator",
                    "task_description": "Do stuff",
                    "owned_files": ["src/main.py", "tests/test_main.py"],
                    "backend": "claude_code",
                },
            ],
            edges=[],
        )
        dag = Planner._plan_to_dag(plan)
        assert dag.nodes["n1"].owned_files == ["src/main.py", "tests/test_main.py"]
        assert dag.nodes["n1"].backend == "claude_code"


# ===================================================================
# 5. _validate_agents tests
# ===================================================================

class TestValidateAgents:
    def test_valid_agents_pass(self, planner):
        plan = OrchestratorPlan(
            reasoning="",
            nodes=[
                {"id": "n1", "agent_type": "planner", "task_description": "Plan"},
                {"id": "n2", "agent_type": "generator", "task_description": "Build"},
            ],
            edges=[{"from": "n1", "to": "n2"}],
        )
        # Should not raise
        planner._validate_agents(plan)

    def test_unregistered_agent_raises(self, planner):
        plan = OrchestratorPlan(
            reasoning="",
            nodes=[
                {"id": "n1", "agent_type": "planner", "task_description": "Plan"},
                {"id": "n2", "agent_type": "nonexistent_agent", "task_description": "???"},
            ],
            edges=[],
        )
        with pytest.raises(ValueError, match="unregistered agent.*nonexistent_agent"):
            planner._validate_agents(plan)


# ===================================================================
# 6. plan() integration tests
# ===================================================================

class TestPlan:
    @pytest.mark.asyncio
    async def test_plan_with_structured_output(self, planner, mock_llm):
        plan_data = _make_plan_data()
        mock_llm.call.return_value = _structured_tool_response(plan_data)

        dag = await planner.plan("Build a REST API")
        assert isinstance(dag, DAG)
        assert len(dag.nodes) == 3
        assert len(dag.edges) == 2

    @pytest.mark.asyncio
    async def test_plan_with_free_text_fallback(self, planner, mock_llm):
        plan_data = _make_plan_data()
        # structured returns None, free text returns plan
        structured_resp = {"role": "assistant", "content": "", "tool_calls": []}
        mock_llm.call.side_effect = [
            structured_resp,       # _plan_structured_output
            _free_text_response(plan_data),  # _plan_free_text
        ]

        dag = await planner.plan("Build a REST API")
        assert isinstance(dag, DAG)
        assert len(dag.nodes) == 3

    @pytest.mark.asyncio
    async def test_plan_with_project_context(self, planner, mock_llm):
        plan_data = _make_plan_data()
        mock_llm.call.return_value = _structured_tool_response(plan_data)

        ctx = {
            "language": "Python",
            "framework": "FastAPI",
            "existing_files": [
                {"path": "src/main.py", "type": "file"},
            ],
        }
        dag = await planner.plan("Add auth", project_context=ctx)
        assert isinstance(dag, DAG)
        # Verify existing_files was popped and restored
        assert "existing_files" in ctx

    @pytest.mark.asyncio
    async def test_plan_with_learning_optimizer_hints(self, planner, mock_llm):
        plan_data = _make_plan_data()
        mock_llm.call.return_value = _structured_tool_response(plan_data)

        optimizer = MagicMock()
        optimizer.get_planning_hints.return_value = "Use TDD approach"
        planner.learning_optimizer = optimizer

        dag = await planner.plan("Build feature")
        optimizer.get_planning_hints.assert_called_once_with("Build feature")
        assert isinstance(dag, DAG)

    @pytest.mark.asyncio
    async def test_plan_learning_optimizer_exception_is_swallowed(self, planner, mock_llm):
        plan_data = _make_plan_data()
        mock_llm.call.return_value = _structured_tool_response(plan_data)

        optimizer = MagicMock()
        optimizer.get_planning_hints.side_effect = RuntimeError("broken")
        planner.learning_optimizer = optimizer

        # Should not raise -- exception is caught and swallowed
        dag = await planner.plan("Build feature")
        assert isinstance(dag, DAG)

    @pytest.mark.asyncio
    async def test_plan_with_skill_registry(self, planner, mock_llm):
        plan_data = _make_plan_data()
        mock_llm.call.return_value = _structured_tool_response(plan_data)

        skills = MagicMock()
        skills.to_prompt_description.return_value = "Available skills: ..."
        planner.skill_registry = skills

        dag = await planner.plan("Build feature")
        skills.to_prompt_description.assert_called_once()
        assert isinstance(dag, DAG)

    @pytest.mark.asyncio
    async def test_plan_timeout_raises(self, planner, mock_llm):
        mock_llm.call.side_effect = TimeoutError("LLM timed out")
        with pytest.raises(TimeoutError, match="LLM timed out"):
            await planner.plan("Build feature")

    @pytest.mark.asyncio
    async def test_plan_unregistered_agent_raises(self, planner, mock_llm):
        bad_plan = {
            "nodes": [
                {"id": "n1", "agent_type": "ghost_agent", "task_description": "oops"},
            ],
            "edges": [],
            "reasoning": "",
        }
        mock_llm.call.return_value = _free_text_response(bad_plan)
        with pytest.raises(ValueError, match="unregistered agent"):
            await planner.plan("Build something weird")


# ===================================================================
# 7. _infer_fallback_edges tests
# ===================================================================

class TestInferFallbackEdges:
    def test_infers_planner_to_all_non_planner(self):
        dag = DAG()
        dag.add_node(DAGNode(id="p1", agent_type="planner", task_description="Plan"))
        dag.add_node(DAGNode(id="g1", agent_type="generator", task_description="Gen"))
        dag.add_node(DAGNode(id="e1", agent_type="evaluator", task_description="Eval"))

        result = _infer_fallback_edges(dag)
        edge_pairs = {(e.from_node, e.to_node) for e in result.edges}
        # planner -> generator, planner -> evaluator
        assert ("p1", "g1") in edge_pairs
        assert ("p1", "e1") in edge_pairs

    def test_infers_generator_to_evaluator(self):
        dag = DAG()
        dag.add_node(DAGNode(id="g1", agent_type="generator", task_description="Gen"))
        dag.add_node(DAGNode(id="e1", agent_type="evaluator", task_description="Eval"))

        result = _infer_fallback_edges(dag)
        edge_pairs = {(e.from_node, e.to_node) for e in result.edges}
        assert ("g1", "e1") in edge_pairs

    def test_infers_generator_to_non_gen_non_plan(self):
        dag = DAG()
        dag.add_node(DAGNode(id="g1", agent_type="generator", task_description="Gen"))
        dag.add_node(DAGNode(id="e1", agent_type="evaluator", task_description="Eval"))

        result = _infer_fallback_edges(dag)
        edge_pairs = {(e.from_node, e.to_node) for e in result.edges}
        assert ("g1", "e1") in edge_pairs

    def test_no_edges_for_all_same_type(self):
        dag = DAG()
        dag.add_node(DAGNode(id="g1", agent_type="generator", task_description="A"))
        dag.add_node(DAGNode(id="g2", agent_type="generator", task_description="B"))

        result = _infer_fallback_edges(dag)
        # All generators, no planner/evaluator: no fallback edges needed
        assert len(result.edges) == 0

    def test_does_not_duplicate_existing_edges(self):
        dag = DAG()
        dag.add_node(DAGNode(id="p1", agent_type="planner", task_description="Plan"))
        dag.add_node(DAGNode(id="g1", agent_type="generator", task_description="Gen"))
        dag.add_edge("p1", "g1")

        result = _infer_fallback_edges(dag)
        # Should not add duplicate (p1, g1)
        assert sum(1 for e in result.edges if e.from_node == "p1" and e.to_node == "g1") == 1

    def test_empty_dag_returns_empty(self):
        dag = DAG()
        result = _infer_fallback_edges(dag)
        assert len(result.nodes) == 0
        assert len(result.edges) == 0

    def test_planner_to_planner_not_added(self):
        dag = DAG()
        dag.add_node(DAGNode(id="p1", agent_type="planner", task_description="Plan A"))
        dag.add_node(DAGNode(id="p2", agent_type="planner", task_description="Plan B"))

        result = _infer_fallback_edges(dag)
        edge_pairs = {(e.from_node, e.to_node) for e in result.edges}
        assert ("p1", "p2") not in edge_pairs
        assert ("p2", "p1") not in edge_pairs


# ===================================================================
# 8. _apply_rename_map tests
# ===================================================================

class TestApplyRenameMap:
    def test_renames_success_criterion_path(self):
        dag = DAG()
        node = DAGNode(
            id="n1",
            agent_type="generator",
            task_description="Create json.py module",
            success_criteria=[
                SuccessCriterion(
                    type=CriterionType.FILE_EXISTS,
                    path="/json.py",
                ),
            ],
        )
        dag.add_node(node)

        _apply_rename_map(dag, {"json": "app_json"})
        crit = dag.nodes["n1"].success_criteria[0]
        assert isinstance(crit, SuccessCriterion)
        assert "/app_json.py" in crit.path

    def test_renames_string_criterion(self):
        dag = DAG()
        node = DAGNode(
            id="n1",
            agent_type="generator",
            task_description="Create json.py module",
            success_criteria=["File /json.py should exist"],
        )
        dag.add_node(node)

        _apply_rename_map(dag, {"json": "app_json"})
        crit = dag.nodes["n1"].success_criteria[0]
        assert isinstance(crit, str)
        assert "/app_json.py" in crit

    def test_renames_task_description(self):
        dag = DAG()
        node = DAGNode(
            id="n1",
            agent_type="generator",
            task_description="Implement json.py for data serialization",
            success_criteria=[],
        )
        dag.add_node(node)

        _apply_rename_map(dag, {"json": "app_json"})
        assert "app_json.py" in dag.nodes["n1"].task_description
        # Note: "app_json.py" contains the substring "json.py", but the
        # replacement was applied correctly -- the original "json.py" was
        # replaced with "app_json.py".
        assert dag.nodes["n1"].task_description == "Implement app_json.py for data serialization"

    def test_no_rename_when_not_needed(self):
        dag = DAG()
        node = DAGNode(
            id="n1",
            agent_type="generator",
            task_description="Implement user auth",
            success_criteria=["tests pass"],
        )
        dag.add_node(node)

        _apply_rename_map(dag, {"json": "app_json"})
        assert dag.nodes["n1"].task_description == "Implement user auth"
        assert dag.nodes["n1"].success_criteria == ["tests pass"]

    def test_preserves_non_criterion_types(self):
        """Non-string, non-SuccessCriterion items pass through unchanged.

        Note: DAGNode._normalize_criteria converts int 42 to str "42",
        so after construction the criteria list contains ["42"].
        """
        dag = DAG()
        node = DAGNode(
            id="n1",
            agent_type="generator",
            task_description="Do work",
            success_criteria=[42],  # not str or SuccessCriterion
        )
        dag.add_node(node)

        # After DAGNode validation, 42 becomes "42"
        assert dag.nodes["n1"].success_criteria == ["42"]

        _apply_rename_map(dag, {"json": "app_json"})
        # Should remain unchanged since "42" has no "json" references
        assert dag.nodes["n1"].success_criteria == ["42"]

    def test_renames_criterion_pattern(self):
        dag = DAG()
        node = DAGNode(
            id="n1",
            agent_type="generator",
            task_description="Create config.py",
            success_criteria=[
                SuccessCriterion(
                    type=CriterionType.FILE_PATTERN,
                    pattern="/config./settings",
                ),
            ],
        )
        dag.add_node(node)

        _apply_rename_map(dag, {"config": "app_config"})
        crit = dag.nodes["n1"].success_criteria[0]
        assert isinstance(crit, SuccessCriterion)
        assert "/app_config." in crit.pattern

    def test_task_description_no_rename_when_embedded_in_word(self):
        """json.py in the middle of a word should not be renamed."""
        dag = DAG()
        node = DAGNode(
            id="n1",
            agent_type="generator",
            task_description="Use ajson.py parser",
            success_criteria=[],
        )
        dag.add_node(node)

        _apply_rename_map(dag, {"json": "app_json"})
        # 'ajson.py' has 'json.py' preceded by 'a' (alnum), so it should NOT be renamed
        assert "ajson.py" in dag.nodes["n1"].task_description

    def test_empty_rename_map_is_noop(self):
        dag = DAG()
        node = DAGNode(
            id="n1",
            agent_type="generator",
            task_description="Build json.py",
            success_criteria=["json.py exists"],
        )
        dag.add_node(node)

        _apply_rename_map(dag, {})
        assert dag.nodes["n1"].task_description == "Build json.py"


# ===================================================================
# 9. plan_from_template tests
# ===================================================================

class TestPlanFromTemplate:
    @pytest.mark.asyncio
    async def test_template_with_valid_agents(self, planner):
        mock_dag = DAG(reasoning="template")
        mock_dag.add_node(DAGNode(
            id="n1", agent_type="generator", task_description="Build",
        ))
        with patch("templates.library.TemplateRegistry") as MockRegistry:
            MockRegistry.return_value.instantiate.return_value = mock_dag
            result = await planner.plan_from_template("build_api", {"feature": "Todo"})
        assert isinstance(result, DAG)
        assert "n1" in result.nodes

    @pytest.mark.asyncio
    async def test_template_with_unregistered_agent_raises(self, planner):
        mock_dag = DAG(reasoning="template")
        mock_dag.add_node(DAGNode(
            id="n1", agent_type="unknown_agent", task_description="???",
        ))
        with patch("templates.library.TemplateRegistry") as MockRegistry:
            MockRegistry.return_value.instantiate.return_value = mock_dag
            with pytest.raises(ValueError, match="unregistered agent"):
                await planner.plan_from_template("bad_template")


# ===================================================================
# 10. _estimate_dag_tokens tests
# ===================================================================

class TestEstimateDagTokens:
    @pytest.mark.asyncio
    async def test_returns_dag_unchanged_when_no_estimator(self, planner):
        dag = DAG()
        dag.add_node(DAGNode(id="n1", agent_type="generator", task_description="Do work"))
        result = await planner._estimate_dag_tokens(dag)
        assert result is dag
        assert result.nodes["n1"].estimated_tokens == 0

    @pytest.mark.asyncio
    async def test_estimates_and_updates_nodes(self, planner):
        from core.token_estimator import TokenEstimateResult

        estimator = MagicMock()
        est1 = TokenEstimateResult(node_id="n1", estimated_tokens=500, estimation_method="heuristic")
        est2 = TokenEstimateResult(node_id="n2", estimated_tokens=1200, estimation_method="heuristic")
        estimator.estimate_nodes_batch = AsyncMock(return_value=[est1, est2])
        planner._token_estimator = estimator

        dag = DAG()
        dag.add_node(DAGNode(id="n1", agent_type="generator", task_description="A"))
        dag.add_node(DAGNode(id="n2", agent_type="evaluator", task_description="B"))

        with patch("core.token_estimator.build_node_context", return_value="ctx"), \
             patch("agent.prompts.SYSTEM_PROMPTS", {"generator": "sys"}):
            result = await planner._estimate_dag_tokens(dag)

        assert result.nodes["n1"].estimated_tokens == 500
        assert result.nodes["n2"].estimated_tokens == 1200

    @pytest.mark.asyncio
    async def test_empty_dag_returns_early(self, planner):
        estimator = MagicMock()
        planner._token_estimator = estimator

        dag = DAG()
        result = await planner._estimate_dag_tokens(dag)
        estimator.estimate_nodes_batch.assert_not_called()
        assert len(result.nodes) == 0
