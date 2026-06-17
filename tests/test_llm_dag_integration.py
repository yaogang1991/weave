"""LLM integration tests for core DAG paths (#469).

These tests make real LLM API calls and require API keys.
Run with: pytest -m integration -v
Default pytest run skips these entirely.
"""
import os

import pytest

pytestmark = pytest.mark.integration


def _has_api_key() -> bool:
    return bool(
        os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY")
    )


@pytest.fixture(autouse=True)
def skip_no_api_key():
    if not _has_api_key():
        pytest.skip("需要 ANTHROPIC_API_KEY 或 OPENAI_API_KEY")


@pytest.mark.asyncio
async def test_orchestrator_generates_valid_dag():
    """IntelligentOrchestrator.plan() generates a structurally valid DAG."""
    from core.config import WeaveConfig
    from core.agent_registry import AgentRegistry
    from session.store import SessionStore
    from orchestrator.intelligent_orchestrator import IntelligentOrchestrator

    config = WeaveConfig.from_env()
    store = SessionStore(config.event_store_path)
    registry = AgentRegistry()

    orchestrator = IntelligentOrchestrator(
        llm_config=config.llm,
        session_store=store,
        agent_registry=registry,
    )

    dag = await orchestrator.plan("Create a hello() function in Python")

    # DAG has at least one node
    assert len(dag.nodes) >= 1

    # Nodes have valid agent types
    valid_types = {"planner", "generator", "evaluator"}
    for node in dag.nodes.values():
        assert node.agent_type in valid_types, (
            f"Invalid agent_type: {node.agent_type}"
        )

    # DAG has no cycles (topological_levels succeeds)
    levels = dag.topological_levels()
    all_nodes_in_levels = {nid for level in levels for nid in level}
    assert all_nodes_in_levels == set(dag.nodes.keys())


@pytest.mark.asyncio
async def test_shortest_dag_path_executes():
    """Execute a simple DAG with real LLM: planner -> generator."""
    import uuid

    from core.config import WeaveConfig
    from core.agent_registry import AgentRegistry
    from core.exceptions import AgentExecutionError
    from core.models import FailureDecision
    from session.store import SessionStore
    from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
    from core.dag_engine import DAGExecutionEngine, DAGEngineConfig
    from agent.backends.builtin import BuiltinBackend
    from agent.backends.registry import BackendRegistry
    from agent.lightweight_llm_caller import LightweightLLMCaller

    config = WeaveConfig.from_env()
    store = SessionStore(config.event_store_path)
    registry = AgentRegistry()

    orchestrator = IntelligentOrchestrator(
        llm_config=config.llm,
        session_store=store,
        agent_registry=registry,
    )

    dag = await orchestrator.plan("Write a Python one-liner: print('hi')")

    session_id = str(uuid.uuid4())[:8]

    # M7.2.5: Build backend stack instead of removed AgentPool.
    lightweight_caller = LightweightLLMCaller(
        config=config.llm,
        session_store=store,
    )
    builtin_backend = BuiltinBackend(
        lightweight_caller=lightweight_caller,
        session_store=store,
        session_id=session_id,
    )
    backend_registry = BackendRegistry(builtin=builtin_backend)

    # Stub for the removed AgentPool.get_executor() path -- should never
    # be reached because backend_registry is provided.
    async def _removed_agent_executor(node, artifacts, **kwargs):
        raise AgentExecutionError(
            "Legacy agent_executor called after M7.2.5 removal."
        )

    async def abort_handler(dag, node_id, error):
        return FailureDecision(action="abort", reasoning="integration test")

    engine = DAGExecutionEngine(
        agent_executor=_removed_agent_executor,
        failure_handler=abort_handler,
        session_id=session_id,
        config=DAGEngineConfig(
            max_parallel=2,
            artifact_path=config.artifact_path,
            default_agent_backend="builtin",
        ),
        backend_registry=backend_registry,
    )

    result = await engine.execute(dag)

    # At least one node should have completed
    completed = [
        n for n in result.nodes.values()
        if n.status.value in ("success", "partial_pass", "warned")
    ]
    assert len(completed) > 0, (
        f"No nodes completed. Statuses: "
        f"{({n.id: n.status.value for n in result.nodes.values()})}"
    )
