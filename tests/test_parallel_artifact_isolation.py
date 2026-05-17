"""Regression tests for parallel node artifact isolation (issue #106).

Verifies that:
1. Each WorkerAgent instance tracks its own artifacts independently
2. AgentPool.get_or_create returns fresh instances (no shared state)
3. Parallel nodes writing to the same directory don't contaminate each other's
   output_artifacts
4. Evaluator only sees the current node's artifacts
"""
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from agent.agent_pool import AgentPool, WorkerAgent
from core.agent_registry import AgentRegistry
from core.config import LLMConfig
from core.models import DAGNode


@pytest.fixture
def registry():
    return AgentRegistry()


@pytest.fixture
def llm_config():
    return LLMConfig(model="test-model", api_key="test-key")


@pytest.fixture
def session_store(tmp_path):
    from session.store import SessionStore
    return SessionStore(base_path=str(tmp_path / "sessions"))


@pytest.fixture
def pool(llm_config, session_store, registry):
    return AgentPool(
        llm_config=llm_config,
        session_store=session_store,
        agent_registry=registry,
    )


class TestFreshInstances:
    def test_get_or_create_returns_different_instances(self, pool):
        """Each call must produce a new WorkerAgent instance."""
        a = pool.get_or_create("generator")
        b = pool.get_or_create("generator")
        assert a is not b

    def test_get_or_create_does_not_cache(self, pool):
        """Repeated calls for the same type return independent instances."""
        instances = [pool.get_or_create("generator") for _ in range(5)]
        ids = [id(i) for i in instances]
        assert len(set(ids)) == 5


class TestArtifactIsolation:
    def test_artifacts_do_not_leak_between_instances(self, pool):
        """Two WorkerAgent instances writing to the same directory
        must track artifacts independently."""
        w1 = pool.get_or_create("generator")
        w2 = pool.get_or_create("generator")

        w1.worker.artifacts.append("handlers.py")
        w2.worker.artifacts.append("formatters.py")

        assert w1.worker.artifacts == ["handlers.py"]
        assert w2.worker.artifacts == ["formatters.py"]

    def test_parallel_executors_produce_isolated_artifacts(self, pool):
        """get_executor creates a fresh worker per node — artifacts are node-local."""
        with patch.object(WorkerAgent, "execute", new_callable=AsyncMock) as mock_exec:
            mock_exec.side_effect = [
                {"status": "completed", "artifacts": ["a.py"]},
                {"status": "completed", "artifacts": ["b.py"]},
                {"status": "completed", "artifacts": ["c.py"]},
            ]

            executor = pool.get_executor("session-1")

            node_a = DAGNode(id="node_a", agent_type="generator", task_description="impl_a")
            node_b = DAGNode(id="node_b", agent_type="generator", task_description="impl_b")
            node_c = DAGNode(id="node_c", agent_type="generator", task_description="impl_c")

            import asyncio

            async def run_all():
                return await asyncio.gather(
                    executor(node_a, []),
                    executor(node_b, []),
                    executor(node_c, []),
                )

            results = asyncio.run(run_all())

        assert results[0]["artifacts"] == ["a.py"]
        assert results[1]["artifacts"] == ["b.py"]
        assert results[2]["artifacts"] == ["c.py"]


class TestEvaluatorArtifactScope:
    def test_lint_only_checks_specified_targets(self, tmp_path):
        """Evaluator lint must only check the files passed as targets,
        never scanning sibling files in the same directory."""
        from evaluator.engine import EvaluatorEngine

        (tmp_path / "a.py").write_text("print('a')\n")
        (tmp_path / "b.py").write_text("import os\nprint('b')\n")
        (tmp_path / "c.py").write_text("print('c')\n")

        engine = EvaluatorEngine(MagicMock())

        # Mock subprocess.run to simulate flake8 results
        def fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        with patch("evaluator.runner.subprocess.run", side_effect=fake_run):
            _, msg_b = engine._run_lint(["b.py"], tmp_path)

        # Must NOT mention a.py or c.py
        assert "a.py" not in msg_b
        assert "c.py" not in msg_b

    def test_dag_node_output_artifacts_isolated(self):
        """DAGNode.output_artifacts must be per-node, not shared."""
        node_a = DAGNode(id="a", agent_type="generator", task_description="impl a")
        node_b = DAGNode(id="b", agent_type="generator", task_description="impl b")

        node_a.output_artifacts = ["a.py"]
        node_b.output_artifacts = ["b.py"]

        assert node_a.output_artifacts == ["a.py"]
        assert node_b.output_artifacts == ["b.py"]

        # Modify one — must not affect the other
        node_a.output_artifacts.append("a2.py")
        assert node_b.output_artifacts == ["b.py"]
