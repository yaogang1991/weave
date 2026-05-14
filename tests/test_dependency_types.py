"""
Tests for hard/soft dependency semantics (#271) and sibling independence (#296).

Covers:
- DependencyType enum and DAGEdge field
- DAG.get_hard_dependencies / get_soft_dependencies
- DAG engine skip logic: hard deps → SKIP, soft deps → continue with warning
- Mixed dependency scenarios
- Template instantiation with dependency_type
- Plan validator dependency_type validation
- adapt_to_failure topology-aware fallback
"""
from unittest.mock import MagicMock

import pytest

from core.models import (
    DAG,
    DAGEdge,
    DAGNode,
    DependencyType,
    FailureDecision,
    NodeStatus,
)
from core.dag_engine import DAGExecutionEngine


# ── Helpers ──────────────────────────────────────────────────────


def _make_node(nid: str, agent_type: str = "generator", **kw) -> DAGNode:
    defaults = {
        "id": nid,
        "agent_type": agent_type,
        "task_description": f"Task for {nid}",
        "max_retries": 0,
    }
    defaults.update(kw)
    return DAGNode(**defaults)


async def _noop_executor(node, artifacts):
    return {"status": "completed", "summary": "done", "artifacts": [], "output": "ok"}


async def _skip_handler(dag, node_id, error):
    return FailureDecision(action="skip", reasoning="skip")


def _make_engine(**overrides):
    defaults = {
        "agent_executor": _noop_executor,
        "failure_handler": _skip_handler,
        "enable_watchdog": False,
        "max_parallel": 5,
    }
    defaults.update(overrides)
    return DAGExecutionEngine(**defaults)


# ── Model Tests ──────────────────────────────────────────────────


class TestDependencyTypeModel:
    """Tests for DependencyType enum and DAGEdge field."""

    def test_default_is_hard(self):
        edge = DAGEdge(from_node="a", to_node="b")
        assert edge.dependency_type == DependencyType.HARD

    def test_explicit_soft(self):
        edge = DAGEdge(from_node="a", to_node="b", dependency_type=DependencyType.SOFT)
        assert edge.dependency_type == DependencyType.SOFT

    def test_dag_add_edge_default_hard(self):
        dag = DAG()
        dag.add_node(_make_node("a"))
        dag.add_node(_make_node("b"))
        dag.add_edge("a", "b")
        assert dag.edges[0].dependency_type == DependencyType.HARD

    def test_dag_add_edge_soft(self):
        dag = DAG()
        dag.add_node(_make_node("a"))
        dag.add_node(_make_node("b"))
        dag.add_edge("a", "b", dependency_type=DependencyType.SOFT)
        assert dag.edges[0].dependency_type == DependencyType.SOFT


class TestDAGDependencyQueries:
    """Tests for DAG.get_hard_dependencies and get_soft_dependencies."""

    def _make_dag(self) -> DAG:
        dag = DAG()
        dag.add_node(_make_node("a"))
        dag.add_node(_make_node("b"))
        dag.add_node(_make_node("c"))
        dag.add_edge("a", "b", dependency_type=DependencyType.HARD)
        dag.add_edge("c", "b", dependency_type=DependencyType.SOFT)
        return dag

    def test_get_dependencies_returns_all(self):
        dag = self._make_dag()
        assert set(dag.get_dependencies("b")) == {"a", "c"}

    def test_get_hard_dependencies_only_hard(self):
        dag = self._make_dag()
        assert dag.get_hard_dependencies("b") == ["a"]

    def test_get_soft_dependencies_only_soft(self):
        dag = self._make_dag()
        assert dag.get_soft_dependencies("b") == ["c"]

    def test_no_dependencies(self):
        dag = DAG()
        dag.add_node(_make_node("x"))
        assert dag.get_hard_dependencies("x") == []
        assert dag.get_soft_dependencies("x") == []

    def test_all_hard(self):
        dag = DAG()
        dag.add_node(_make_node("a"))
        dag.add_node(_make_node("b"))
        dag.add_node(_make_node("c"))
        dag.add_edge("a", "c")
        dag.add_edge("b", "c")
        assert set(dag.get_hard_dependencies("c")) == {"a", "b"}
        assert dag.get_soft_dependencies("c") == []

    def test_all_soft(self):
        dag = DAG()
        dag.add_node(_make_node("a"))
        dag.add_node(_make_node("b"))
        dag.add_node(_make_node("c"))
        dag.add_edge("a", "c", dependency_type=DependencyType.SOFT)
        dag.add_edge("b", "c", dependency_type=DependencyType.SOFT)
        assert dag.get_hard_dependencies("c") == []
        assert set(dag.get_soft_dependencies("c")) == {"a", "b"}


# ── DAG Engine Tests ─────────────────────────────────────────────
# NOTE: The failure_handler returning "skip" changes node status from
# FAILED → SKIPPED. Tests assert the post-handler final state.


class TestHardDependencySkip:
    """Hard dependency upstream FAILED → downstream SKIP."""

    @pytest.mark.asyncio
    async def test_hard_dep_failed_skips_downstream(self):
        """A→B(hard), A fails → A becomes SKIPPED (via handler), B SKIP."""
        dag = DAG()
        dag.add_node(_make_node("a"))
        dag.add_node(_make_node("b"))
        dag.add_edge("a", "b", dependency_type=DependencyType.HARD)

        async def fail_a(node, artifacts):
            if node.id == "a":
                raise RuntimeError("A failed")
            return {"artifacts": [], "output": "ok"}

        engine = _make_engine(agent_executor=fail_a)
        result = await engine.execute(dag)

        # A failed, handler skipped it → SKIPPED; B skipped due to hard dep
        assert result.nodes["a"].status == NodeStatus.SKIPPED
        assert result.nodes["b"].status == NodeStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_hard_dep_succeeded_allows_downstream(self):
        """A→B(hard), A succeeds → B executes."""
        dag = DAG()
        dag.add_node(_make_node("a"))
        dag.add_node(_make_node("b"))
        dag.add_edge("a", "b", dependency_type=DependencyType.HARD)

        engine = _make_engine()
        result = await engine.execute(dag)

        assert result.nodes["a"].status == NodeStatus.SUCCESS
        assert result.nodes["b"].status == NodeStatus.SUCCESS


class TestSoftDependencyContinue:
    """Soft dependency upstream FAILED → downstream continues."""

    @pytest.mark.asyncio
    async def test_soft_dep_failed_continues(self):
        """A→B(soft), A fails → B still executes."""
        dag = DAG()
        dag.add_node(_make_node("a"))
        dag.add_node(_make_node("b"))
        dag.add_edge("a", "b", dependency_type=DependencyType.SOFT)

        async def fail_a(node, artifacts):
            if node.id == "a":
                raise RuntimeError("A failed")
            return {"artifacts": [], "output": "ok"}

        engine = _make_engine(agent_executor=fail_a)
        result = await engine.execute(dag)

        # A failed, handler skipped it → SKIPPED; B has soft dep → still runs
        assert result.nodes["a"].status == NodeStatus.SKIPPED
        assert result.nodes["b"].status == NodeStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_soft_dep_chain(self):
        """A→B(soft)→C(soft), A fails → B and C both execute."""
        dag = DAG()
        dag.add_node(_make_node("a"))
        dag.add_node(_make_node("b"))
        dag.add_node(_make_node("c"))
        dag.add_edge("a", "b", dependency_type=DependencyType.SOFT)
        dag.add_edge("b", "c", dependency_type=DependencyType.SOFT)

        async def fail_a(node, artifacts):
            if node.id == "a":
                raise RuntimeError("A failed")
            return {"artifacts": [], "output": "ok"}

        engine = _make_engine(agent_executor=fail_a)
        result = await engine.execute(dag)

        assert result.nodes["a"].status == NodeStatus.SKIPPED
        assert result.nodes["b"].status == NodeStatus.SUCCESS
        assert result.nodes["c"].status == NodeStatus.SUCCESS


class TestMixedDependencies:
    """Mixed hard/soft dependency scenarios."""

    @pytest.mark.asyncio
    async def test_hard_fails_soft_ok_skips(self):
        """A→C(hard), B→C(soft). A fails, B succeeds → C SKIP (hard dep failed)."""
        dag = DAG()
        dag.add_node(_make_node("a"))
        dag.add_node(_make_node("b"))
        dag.add_node(_make_node("c"))
        dag.add_edge("a", "c", dependency_type=DependencyType.HARD)
        dag.add_edge("b", "c", dependency_type=DependencyType.SOFT)

        async def fail_a(node, artifacts):
            if node.id == "a":
                raise RuntimeError("A failed")
            return {"artifacts": [], "output": "ok"}

        engine = _make_engine(agent_executor=fail_a)
        result = await engine.execute(dag)

        assert result.nodes["a"].status == NodeStatus.SKIPPED
        assert result.nodes["b"].status == NodeStatus.SUCCESS
        assert result.nodes["c"].status == NodeStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_hard_ok_soft_fails_continues(self):
        """A→C(hard), B→C(soft). A succeeds, B fails → C executes (hard dep OK)."""
        dag = DAG()
        dag.add_node(_make_node("a"))
        dag.add_node(_make_node("b"))
        dag.add_node(_make_node("c"))
        dag.add_edge("a", "c", dependency_type=DependencyType.HARD)
        dag.add_edge("b", "c", dependency_type=DependencyType.SOFT)

        async def fail_b(node, artifacts):
            if node.id == "b":
                raise RuntimeError("B failed")
            return {"artifacts": [], "output": "ok"}

        engine = _make_engine(agent_executor=fail_b)
        result = await engine.execute(dag)

        assert result.nodes["a"].status == NodeStatus.SUCCESS
        assert result.nodes["b"].status == NodeStatus.SKIPPED
        assert result.nodes["c"].status == NodeStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_all_soft_deps_fail_continues(self):
        """A→C(soft), B→C(soft). Both A and B fail → C still executes."""
        dag = DAG()
        dag.add_node(_make_node("a"))
        dag.add_node(_make_node("b"))
        dag.add_node(_make_node("c"))
        dag.add_edge("a", "c", dependency_type=DependencyType.SOFT)
        dag.add_edge("b", "c", dependency_type=DependencyType.SOFT)

        async def fail_ab(node, artifacts):
            if node.id in ("a", "b"):
                raise RuntimeError(f"{node.id} failed")
            return {"artifacts": [], "output": "ok"}

        engine = _make_engine(agent_executor=fail_ab)
        result = await engine.execute(dag)

        assert result.nodes["a"].status == NodeStatus.SKIPPED
        assert result.nodes["b"].status == NodeStatus.SKIPPED
        assert result.nodes["c"].status == NodeStatus.SUCCESS


class TestSiblingIndependence:
    """Sibling nodes with different dependency types."""

    @pytest.mark.asyncio
    async def test_siblings_with_soft_deps_run_independently(self):
        """Foundation→A(soft), Foundation→B(hard), Foundation→C(hard).
        A fails → B and C still execute."""
        dag = DAG()
        dag.add_node(_make_node("foundation"))
        dag.add_node(_make_node("a"))
        dag.add_node(_make_node("b"))
        dag.add_node(_make_node("c"))
        dag.add_edge("foundation", "a", dependency_type=DependencyType.SOFT)
        dag.add_edge("foundation", "b", dependency_type=DependencyType.HARD)
        dag.add_edge("foundation", "c", dependency_type=DependencyType.HARD)

        async def fail_a(node, artifacts):
            if node.id == "a":
                raise RuntimeError("A failed")
            return {"artifacts": [], "output": "ok"}

        engine = _make_engine(agent_executor=fail_a)
        result = await engine.execute(dag)

        assert result.nodes["foundation"].status == NodeStatus.SUCCESS
        assert result.nodes["a"].status == NodeStatus.SKIPPED
        assert result.nodes["b"].status == NodeStatus.SUCCESS
        assert result.nodes["c"].status == NodeStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_parallel_siblings_independent_failure(self):
        """6 parallel Level 1 nodes, one fails, others continue (#296).
        impl_accounts fails → others NOT skipped, integration SKIP (hard dep)."""
        dag = DAG()
        dag.add_node(_make_node("foundation", agent_type="planner"))
        for name in ["impl_core", "impl_accounts", "impl_trans", "impl_budgets", "impl_reports"]:
            dag.add_node(_make_node(name))
        dag.add_node(_make_node("integration", agent_type="evaluator"))

        for name in ["impl_core", "impl_accounts", "impl_trans", "impl_budgets", "impl_reports"]:
            dag.add_edge("foundation", name)
        for name in ["impl_core", "impl_accounts", "impl_trans", "impl_budgets", "impl_reports"]:
            dag.add_edge(name, "integration", dependency_type=DependencyType.HARD)

        async def selective_fail(node, artifacts):
            if node.id == "impl_accounts":
                raise RuntimeError("accounts failed")
            return {"artifacts": [], "output": "ok"}

        engine = _make_engine(agent_executor=selective_fail)
        result = await engine.execute(dag)

        assert result.nodes["impl_accounts"].status == NodeStatus.SKIPPED
        assert result.nodes["impl_core"].status == NodeStatus.SUCCESS
        assert result.nodes["impl_trans"].status == NodeStatus.SUCCESS
        assert result.nodes["impl_budgets"].status == NodeStatus.SUCCESS
        assert result.nodes["impl_reports"].status == NodeStatus.SUCCESS
        # Integration has hard dep on impl_accounts → SKIP
        assert result.nodes["integration"].status == NodeStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_parallel_siblings_soft_downstream(self):
        """integration has soft deps on impl nodes.
        impl_accounts fails → integration still runs."""
        dag = DAG()
        dag.add_node(_make_node("foundation", agent_type="planner"))
        for name in ["impl_core", "impl_accounts", "impl_trans"]:
            dag.add_node(_make_node(name))
        dag.add_node(_make_node("integration", agent_type="evaluator"))

        for name in ["impl_core", "impl_accounts", "impl_trans"]:
            dag.add_edge("foundation", name)
        for name in ["impl_core", "impl_accounts", "impl_trans"]:
            dag.add_edge(name, "integration", dependency_type=DependencyType.SOFT)

        async def selective_fail(node, artifacts):
            if node.id == "impl_accounts":
                raise RuntimeError("accounts failed")
            return {"artifacts": [], "output": "ok"}

        engine = _make_engine(agent_executor=selective_fail)
        result = await engine.execute(dag)

        assert result.nodes["impl_accounts"].status == NodeStatus.SKIPPED
        assert result.nodes["impl_core"].status == NodeStatus.SUCCESS
        assert result.nodes["impl_trans"].status == NodeStatus.SUCCESS
        assert result.nodes["integration"].status == NodeStatus.SUCCESS


class TestBackwardCompatibility:
    """Ensure default (no dependency_type specified) behaves as before."""

    @pytest.mark.asyncio
    async def test_default_edge_is_hard(self):
        """Edge without dependency_type defaults to hard → skip on failure."""
        dag = DAG()
        dag.add_node(_make_node("a"))
        dag.add_node(_make_node("b"))
        dag.add_edge("a", "b")

        async def fail_a(node, artifacts):
            if node.id == "a":
                raise RuntimeError("A failed")
            return {"artifacts": [], "output": "ok"}

        engine = _make_engine(agent_executor=fail_a)
        result = await engine.execute(dag)

        assert result.nodes["a"].status == NodeStatus.SKIPPED
        assert result.nodes["b"].status == NodeStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_existing_cascade_skip_still_works(self):
        """Linear chain A→B→C, A fails → B and C skipped (existing behavior)."""
        dag = DAG()
        dag.add_node(_make_node("a"))
        dag.add_node(_make_node("b"))
        dag.add_node(_make_node("c"))
        dag.add_edge("a", "b")
        dag.add_edge("b", "c")

        async def fail_a(node, artifacts):
            if node.id == "a":
                raise RuntimeError("A failed")
            return {"artifacts": [], "output": "ok"}

        engine = _make_engine(agent_executor=fail_a)
        result = await engine.execute(dag)

        assert result.nodes["a"].status == NodeStatus.SKIPPED
        assert result.nodes["b"].status == NodeStatus.SKIPPED
        assert result.nodes["c"].status == NodeStatus.SKIPPED


class TestTemplateDependencyType:
    """Template instantiation with dependency_type."""

    def test_instantiate_with_soft_edge(self):
        from templates.library import TemplateRegistry
        import tempfile
        import yaml

        tpl_data = {
            "name": "test_soft",
            "description": "Test soft dep",
            "version": "1.0",
            "category": "test",
            "variables": {},
            "nodes": [
                {"id": "a", "agent_type": "generator", "task_description": "do a"},
                {"id": "b", "agent_type": "evaluator", "task_description": "eval b"},
            ],
            "edges": [
                {"from": "a", "to": "b", "dependency_type": "soft"},
            ],
            "reasoning_template": "test",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            tpl_path = f"{tmpdir}/test_soft.yaml"
            with open(tpl_path, "w") as f:
                yaml.dump(tpl_data, f)

            registry = TemplateRegistry(templates_dir=tmpdir)
            dag = registry.instantiate("test_soft")

            assert len(dag.edges) == 1
            assert dag.edges[0].dependency_type == DependencyType.SOFT
            assert dag.edges[0].from_node == "a"
            assert dag.edges[0].to_node == "b"

    def test_instantiate_default_hard_edge(self):
        from templates.library import TemplateRegistry
        import tempfile
        import yaml

        tpl_data = {
            "name": "test_hard",
            "description": "Test hard dep",
            "version": "1.0",
            "category": "test",
            "variables": {},
            "nodes": [
                {"id": "x", "agent_type": "generator", "task_description": "do x"},
                {"id": "y", "agent_type": "evaluator", "task_description": "eval y"},
            ],
            "edges": [
                {"from": "x", "to": "y"},
            ],
            "reasoning_template": "test",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            tpl_path = f"{tmpdir}/test_hard.yaml"
            with open(tpl_path, "w") as f:
                yaml.dump(tpl_data, f)

            registry = TemplateRegistry(templates_dir=tmpdir)
            dag = registry.instantiate("test_hard")

            assert len(dag.edges) == 1
            assert dag.edges[0].dependency_type == DependencyType.HARD


class TestPlanValidatorDependencyType:
    """Plan validator checks dependency_type values."""

    def test_valid_hard_edge(self):
        from orchestrator.plan_validator import PlanValidator

        plan = {
            "nodes": [
                {"id": "a", "agent_type": "generator", "task": "do a"},
                {"id": "b", "agent_type": "evaluator", "task": "eval"},
            ],
            "edges": [
                {"from": "a", "to": "b", "dependency_type": "hard"},
            ],
        }
        validator = PlanValidator()
        result = validator.validate(plan)
        assert result is not None

    def test_valid_soft_edge(self):
        from orchestrator.plan_validator import PlanValidator

        plan = {
            "nodes": [
                {"id": "a", "agent_type": "generator", "task": "do a"},
                {"id": "b", "agent_type": "evaluator", "task": "eval"},
            ],
            "edges": [
                {"from": "a", "to": "b", "dependency_type": "soft"},
            ],
        }
        validator = PlanValidator()
        result = validator.validate(plan)
        assert result is not None

    def test_invalid_dependency_type_raises(self):
        from orchestrator.plan_validator import PlanValidator, PlanValidationError

        plan = {
            "nodes": [
                {"id": "a", "agent_type": "generator", "task": "do a"},
                {"id": "b", "agent_type": "evaluator", "task": "eval"},
            ],
            "edges": [
                {"from": "a", "to": "b", "dependency_type": "invalid"},
            ],
        }
        validator = PlanValidator()
        with pytest.raises(PlanValidationError, match="Invalid dependency_type"):
            validator.validate(plan)

    def test_no_dependency_type_defaults_valid(self):
        from orchestrator.plan_validator import PlanValidator

        plan = {
            "nodes": [
                {"id": "a", "agent_type": "generator", "task": "do a"},
                {"id": "b", "agent_type": "evaluator", "task": "eval"},
            ],
            "edges": [
                {"from": "a", "to": "b"},
            ],
        }
        validator = PlanValidator()
        result = validator.validate(plan)
        assert result is not None


class TestAdaptToFailureSoftFallback:
    """adapt_to_failure with soft-only dependents defaults to skip on parse error."""

    @pytest.mark.asyncio
    async def test_soft_only_dependents_skip_fallback(self):
        """Failed node with only soft dependents → skip (not abort) on parse error."""
        dag = DAG()
        dag.add_node(_make_node("a"))
        dag.add_node(_make_node("b"))
        dag.add_edge("a", "b", dependency_type=DependencyType.SOFT)
        dag.nodes["a"].status = NodeStatus.FAILED
        dag.nodes["a"].error = "boom"
        dag.nodes["a"].retry_count = 99

        from core.config import LLMConfig
        from core.llm_client import LLMClient
        from session.store import SessionStore

        mock_llm = MagicMock(spec=LLMClient)
        mock_llm.call.return_value = {"content": "NOT JSON"}

        from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
        from core.agent_registry import AgentRegistry

        llm_config = LLMConfig()
        registry = AgentRegistry()
        session_store = SessionStore()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=session_store,
            agent_registry=registry,
        )
        orch.llm = mock_llm

        decision = await orch.adapt_to_failure(dag, "a", "boom")
        assert decision.action == "skip"

    @pytest.mark.asyncio
    async def test_hard_dependents_abort_fallback(self):
        """Failed node with hard dependents → abort on parse error."""
        dag = DAG()
        dag.add_node(_make_node("a"))
        dag.add_node(_make_node("b"))
        dag.add_edge("a", "b", dependency_type=DependencyType.HARD)
        dag.nodes["a"].status = NodeStatus.FAILED
        dag.nodes["a"].error = "boom"
        dag.nodes["a"].retry_count = 99

        from core.config import LLMConfig
        from core.llm_client import LLMClient
        from session.store import SessionStore

        mock_llm = MagicMock(spec=LLMClient)
        mock_llm.call.return_value = {"content": "NOT JSON"}

        from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
        from core.agent_registry import AgentRegistry

        llm_config = LLMConfig()
        registry = AgentRegistry()
        session_store = SessionStore()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=session_store,
            agent_registry=registry,
        )
        orch.llm = mock_llm

        decision = await orch.adapt_to_failure(dag, "a", "boom")
        assert decision.action == "abort"

    @pytest.mark.asyncio
    async def test_no_dependents_abort_fallback(self):
        """Failed node with no dependents → abort on parse error."""
        dag = DAG()
        dag.add_node(_make_node("a"))
        dag.nodes["a"].status = NodeStatus.FAILED
        dag.nodes["a"].error = "boom"
        dag.nodes["a"].retry_count = 99

        from core.config import LLMConfig
        from core.llm_client import LLMClient
        from session.store import SessionStore

        mock_llm = MagicMock(spec=LLMClient)
        mock_llm.call.return_value = {"content": "NOT JSON"}

        from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
        from core.agent_registry import AgentRegistry

        llm_config = LLMConfig()
        registry = AgentRegistry()
        session_store = SessionStore()
        orch = IntelligentOrchestrator(
            llm_config=llm_config,
            session_store=session_store,
            agent_registry=registry,
        )
        orch.llm = mock_llm

        decision = await orch.adapt_to_failure(dag, "a", "boom")
        assert decision.action == "abort"
