"""
Tests for M3.4 DAG Template System.

Covers: DAGTemplate model, TemplateRegistry, YAML loading,
variable substitution, orchestrator plan_from_template(), CLI, and API.
"""

import json
import pytest
from unittest.mock import MagicMock

from core.models import DAGTemplate, DAG
from templates.library import TemplateRegistry


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def builtin_registry():
    return TemplateRegistry()


@pytest.fixture
def custom_registry(tmp_path):
    """Registry pointing to a temp dir for custom template tests."""
    return TemplateRegistry(templates_dir=str(tmp_path))


@pytest.fixture
def sample_yaml(tmp_path):
    """Write a minimal valid template YAML."""
    content = """
name: test_template
description: A test template
version: "2.0"
category: test
variables:
  target: world
nodes:
  - id: planner
    agent_type: planner
    task_description: "Plan for {target}"
  - id: generator
    agent_type: generator
    task_description: "Build {target}"
edges:
  - from: planner
    to: generator
reasoning_template: "Building {target}"
"""
    path = tmp_path / "test_template.yaml"
    path.write_text(content)
    return path


# =============================================================================
# TestDAGTemplateModel
# =============================================================================


class TestDAGTemplateModel:
    def test_default_fields(self):
        tpl = DAGTemplate(
            name="test",
            description="desc",
            nodes=[{"id": "n1", "agent_type": "planner", "task_description": "t"}],
        )
        assert tpl.version == "1.0"
        assert tpl.category == "general"
        assert tpl.variables == {}
        assert tpl.edges == []
        assert tpl.reasoning_template == ""

    def test_full_fields(self):
        tpl = DAGTemplate(
            name="build",
            description="Build something",
            version="2.0",
            category="backend",
            variables={"feature": "API"},
            nodes=[
                {"id": "n1", "agent_type": "planner", "task_description": "Plan"},
            ],
            edges=[{"from": "n1", "to": "n2"}],
            reasoning_template="Plan {feature}",
        )
        assert tpl.name == "build"
        assert tpl.variables["feature"] == "API"

    def test_serialization_roundtrip(self):
        tpl = DAGTemplate(
            name="test",
            description="desc",
            nodes=[
                {"id": "n1", "agent_type": "planner", "task_description": "Plan {x}"},
            ],
            variables={"x": "default"},
        )
        data = tpl.model_dump()
        restored = DAGTemplate(**data)
        assert restored.name == tpl.name
        assert restored.nodes[0]["task_description"] == "Plan {x}"


# =============================================================================
# TestTemplateRegistryListAndLoad
# =============================================================================


class TestTemplateRegistryListAndLoad:
    def test_list_builtin_templates(self, builtin_registry):
        templates = builtin_registry.list_templates()
        assert len(templates) == 7
        names = {t.name for t in templates}
        assert "build_api" in names
        assert "add_feature" in names
        assert "fix_bug" in names
        assert "refactor" in names
        assert "add_tests" in names
        assert "add_auth" in names
        assert "setup_project" in names

    def test_get_template_found(self, builtin_registry):
        tpl = builtin_registry.get_template("build_api")
        assert tpl is not None
        assert tpl.name == "build_api"
        assert len(tpl.nodes) == 4
        assert len(tpl.edges) == 3

    def test_get_template_not_found(self, builtin_registry):
        tpl = builtin_registry.get_template("nonexistent")
        assert tpl is None

    def test_get_template_caches(self, builtin_registry):
        tpl1 = builtin_registry.get_template("build_api")
        tpl2 = builtin_registry.get_template("build_api")
        assert tpl1 is tpl2  # Same object from cache

    def test_list_empty_dir(self, custom_registry):
        templates = custom_registry.list_templates()
        assert templates == []

    def test_list_nonexistent_dir(self, tmp_path):
        reg = TemplateRegistry(templates_dir=str(tmp_path / "nonexistent"))
        assert reg.list_templates() == []

    def test_load_custom_yaml(self, custom_registry, sample_yaml):
        tpl = custom_registry.get_template("test_template")
        assert tpl is not None
        assert tpl.name == "test_template"
        assert tpl.version == "2.0"
        assert tpl.category == "test"

    def test_load_invalid_yaml(self, custom_registry, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("not a mapping")
        templates = custom_registry.list_templates()
        # Should skip invalid and return empty
        assert isinstance(templates, list)

    def test_load_yaml_missing_fields(self, custom_registry, tmp_path):
        path = tmp_path / "incomplete.yaml"
        path.write_text("name: foo\n")  # Missing nodes
        templates = custom_registry.list_templates()
        assert isinstance(templates, list)

    def test_load_yml_extension(self, custom_registry, tmp_path):
        path = tmp_path / "test.yml"
        path.write_text("""
name: yml_test
description: Test .yml extension
nodes:
  - id: n1
    agent_type: planner
    task_description: "Test"
""")
        tpl = custom_registry.get_template("test")
        assert tpl is not None
        assert tpl.name == "yml_test"


# =============================================================================
# TestTemplateInstantiation
# =============================================================================


class TestTemplateInstantiation:
    def test_instantiate_builtin(self, builtin_registry):
        dag = builtin_registry.instantiate("build_api", {
            "feature": "Todo API",
            "language": "Python",
        })
        assert isinstance(dag, DAG)
        assert len(dag.nodes) == 4
        assert len(dag.edges) == 3
        assert "Todo API" in dag.nodes["planner_api"].task_description

    def test_instantiate_with_defaults(self, builtin_registry):
        dag = builtin_registry.instantiate("build_api")
        # Should use template default variables
        assert "API endpoint" in dag.nodes["planner_api"].task_description

    def test_instantiate_not_found(self, builtin_registry):
        with pytest.raises(ValueError, match="Template not found"):
            builtin_registry.instantiate("nonexistent")

    def test_variable_substitution_in_nodes(self, builtin_registry):
        dag = builtin_registry.instantiate("fix_bug", {"bug": "null pointer"})
        # Only the planner node references {bug}
        planner = dag.nodes["planner_analyze"]
        assert "{bug}" not in planner.task_description
        assert "null pointer" in planner.task_description
        # Ensure no unresolved placeholders remain anywhere
        for nid, node in dag.nodes.items():
            assert "{bug}" not in node.task_description

    def test_variable_substitution_in_reasoning(self, builtin_registry):
        dag = builtin_registry.instantiate("add_feature", {"feature": "search"})
        assert "search" in dag.reasoning

    def test_unresolved_variables_remain(self, builtin_registry):
        dag = builtin_registry.instantiate("add_feature")
        # Default variable is "new feature"
        assert "new feature" in dag.nodes["planner"].task_description

    def test_edges_created_correctly(self, builtin_registry):
        dag = builtin_registry.instantiate("build_api", {
            "feature": "API", "language": "Python",
        })
        edge_pairs = [(e.from_node, e.to_node) for e in dag.edges]
        assert ("planner_api", "generator_api") in edge_pairs
        assert ("generator_api", "generator_tests") in edge_pairs
        assert ("generator_tests", "evaluator") in edge_pairs

    def test_dag_is_valid(self, builtin_registry):
        dag = builtin_registry.instantiate("build_api")
        levels = dag.topological_levels()
        assert len(levels) >= 2
        # First level should have planner
        assert "planner_api" in levels[0]
        # Last level should have evaluator
        assert "evaluator" in levels[-1]

    def test_all_builtin_templates_instantiate(self, builtin_registry):
        """Verify every built-in template instantiates without error."""
        for tpl in builtin_registry.list_templates():
            dag = builtin_registry.instantiate(tpl.name)
            assert isinstance(dag, DAG)
            assert len(dag.nodes) >= 2  # At least generator + evaluator

    def test_custom_template_instantiate(self, custom_registry, sample_yaml):
        dag = custom_registry.instantiate("test_template", {"target": "universe"})
        assert len(dag.nodes) == 2
        assert "universe" in dag.nodes["planner"].task_description
        assert "universe" in dag.nodes["generator"].task_description
        assert "universe" in dag.reasoning


# =============================================================================
# TestSubstitute
# =============================================================================


class TestSubstitute:
    def test_basic_substitution(self, builtin_registry):
        result = builtin_registry._substitute("Hello {name}", {"name": "World"})
        assert result == "Hello World"

    def test_multiple_vars(self, builtin_registry):
        result = builtin_registry._substitute(
            "{a} and {b}", {"a": "X", "b": "Y"},
        )
        assert result == "X and Y"

    def test_unresolved_kept(self, builtin_registry):
        result = builtin_registry._substitute("Hello {name}", {})
        assert result == "Hello {name}"

    def test_no_placeholders(self, builtin_registry):
        result = builtin_registry._substitute("No vars here", {"x": "y"})
        assert result == "No vars here"

    def test_partial_substitution(self, builtin_registry):
        result = builtin_registry._substitute(
            "{a} {b} {c}", {"a": "X", "c": "Z"},
        )
        assert result == "X {b} Z"


# =============================================================================
# TestOrchestratorIntegration
# =============================================================================


class TestOrchestratorIntegration:
    def _make_orchestrator(self, agent_registry=None):
        from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
        from core.agent_registry import AgentRegistry
        from session.store import SessionStore
        from unittest.mock import MagicMock

        registry = agent_registry or AgentRegistry()
        store = MagicMock(spec=SessionStore)
        llm_client = MagicMock()
        llm_router = MagicMock()
        llm_router.get_client.return_value = llm_client

        orchestrator = IntelligentOrchestrator(
            llm_config=MagicMock(),
            session_store=store,
            agent_registry=registry,
            llm_router=llm_router,
        )
        return orchestrator

    @pytest.mark.asyncio
    async def test_plan_from_template(self):
        orchestrator = self._make_orchestrator()

        dag = await orchestrator.plan_from_template(
            "build_api",
            {"feature": "User API", "language": "Python"},
        )
        assert isinstance(dag, DAG)
        assert len(dag.nodes) == 4
        assert "User API" in dag.nodes["planner_api"].task_description

    @pytest.mark.asyncio
    async def test_plan_from_template_invalid_agent(self):
        """Template with agent_type not in registry should raise."""
        registry = MagicMock()
        registry.list_agents.return_value = []
        registry.has_agent.return_value = False

        orchestrator = self._make_orchestrator(agent_registry=registry)

        with pytest.raises(ValueError, match="unregistered agent"):
            await orchestrator.plan_from_template("build_api")

    @pytest.mark.asyncio
    async def test_plan_from_template_not_found(self):
        orchestrator = self._make_orchestrator()

        with pytest.raises(ValueError, match="Template not found"):
            await orchestrator.plan_from_template("nonexistent_template_xyz")


# =============================================================================
# TestCLIIntegration
# =============================================================================


class TestCLIIntegration:
    def test_templates_command_no_args(self):
        """templates command without --name lists all templates."""
        import subprocess
        result = subprocess.run(
            [".venv/bin/python", "main.py", "templates"],
            capture_output=True, text=True, cwd="/Users/yaogang/project/harness",
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["count"] == 7
        assert len(data["templates"]) == 7

    def test_templates_command_with_name(self):
        """templates --name shows specific template details."""
        import subprocess
        result = subprocess.run(
            [".venv/bin/python", "main.py", "templates", "--name", "fix_bug"],
            capture_output=True, text=True, cwd="/Users/yaogang/project/harness",
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["name"] == "fix_bug"
        assert data["category"] == "maintenance"

    def test_templates_command_not_found(self):
        """templates --name with nonexistent template errors."""
        import subprocess
        result = subprocess.run(
            [".venv/bin/python", "main.py", "templates", "--name", "nope"],
            capture_output=True, text=True, cwd="/Users/yaogang/project/harness",
        )
        assert result.returncode != 0

    def test_parse_template_vars(self):
        from main import _parse_template_vars
        result = _parse_template_vars(["feature=API", "language=Python"])
        assert result == {"feature": "API", "language": "Python"}

    def test_parse_template_vars_empty(self):
        from main import _parse_template_vars
        assert _parse_template_vars([]) == {}

    def test_parse_template_vars_with_equals_in_value(self):
        from main import _parse_template_vars
        result = _parse_template_vars(["cmd=echo hello=world"])
        assert result == {"cmd": "echo hello=world"}


# =============================================================================
# TestTemplateCategories
# =============================================================================


class TestTemplateCategories:
    def test_categories_diverse(self, builtin_registry):
        templates = builtin_registry.list_templates()
        categories = {t.category for t in templates}
        assert len(categories) >= 4  # backend, general, maintenance, quality, security, scaffolding

    def test_each_template_has_reasoning(self, builtin_registry):
        for tpl in builtin_registry.list_templates():
            assert tpl.reasoning_template, f"{tpl.name} missing reasoning_template"

    def test_each_template_edges_reference_valid_nodes(self, builtin_registry):
        for tpl in builtin_registry.list_templates():
            node_ids = {n["id"] for n in tpl.nodes}
            for edge in tpl.edges:
                assert edge["from"] in node_ids, (
                    f"{tpl.name}: edge from '{edge['from']}' not in nodes"
                )
                assert edge["to"] in node_ids, (
                    f"{tpl.name}: edge to '{edge['to']}' not in nodes"
                )

    def test_each_template_has_evaluator(self, builtin_registry):
        """Every template should end with an evaluator node."""
        for tpl in builtin_registry.list_templates():
            agent_types = {n["agent_type"] for n in tpl.nodes}
            assert "evaluator" in agent_types, (
                f"{tpl.name} has no evaluator node"
            )
