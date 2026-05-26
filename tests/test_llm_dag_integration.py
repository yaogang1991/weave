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
    """Execute a simple DAG with real LLM: planner → generator."""
    from core.config import WeaveConfig
    from core.agent_registry import AgentRegistry
    from session.store import SessionStore
    from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
    from core.dag_engine import DAGExecutionEngine, DAGEngineConfig
    from agent.agent_pool import AgentPool
    from core.models import FailureDecision
    from tools.registry import ToolRegistry

    config = WeaveConfig.from_env()
    store = SessionStore(config.event_store_path)
    registry = AgentRegistry()
    tool_registry = ToolRegistry()

    orchestrator = IntelligentOrchestrator(
        llm_config=config.llm,
        session_store=store,
        agent_registry=registry,
    )

    dag = await orchestrator.plan("Write a Python one-liner: print('hi')")

    # Build engine with real agent pool
    import uuid

    session_id = str(uuid.uuid4())[:8]
    pool = AgentPool(
        llm_config=config.llm,
        session_store=store,
        agent_registry=registry,
        tool_registry=tool_registry,
        max_iterations=3,
    )

    async def abort_handler(dag, node_id, error):
        return FailureDecision(action="abort", reasoning="integration test")

    engine = DAGExecutionEngine(
        agent_executor=pool.get_executor(session_id),
        failure_handler=abort_handler,
        session_id=session_id,
        config=DAGEngineConfig(
            max_parallel=2,
            artifact_path=config.artifact_path,
        ),
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
