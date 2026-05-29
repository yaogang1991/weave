"""Tests for #828: print failed node details on DAG execution failure."""

from unittest.mock import MagicMock


from core.dag_models import DAG, DAGNode, DAGEdge, NodeStatus


def _make_node(nid, status=NodeStatus.FAILED, error=""):
    return DAGNode(id=nid, agent_type="generator", task_description=f"task {nid}",
                   status=status, error=error)


class TestFailureOutput:
    def test_failed_node_details_printed(self, capsys):
        from cli.execution import _finalize_execution

        engine = MagicMock()
        engine.get_execution_summary.return_value = {
            "total_nodes": 4, "success": 1, "failed": 1,
            "skipped": 1, "implementation_success": 0,
            "implementation_total": 2,
        }
        dag = DAG(
            nodes={
                "a": _make_node("a", NodeStatus.SUCCESS),
                "b": _make_node("b", NodeStatus.FAILED, "import smoke test failed"),
                "c": _make_node("c", NodeStatus.SKIPPED, "dependency b failed"),
                "d": _make_node("d", NodeStatus.PENDING),
            },
            edges=[DAGEdge(from_node="a", to_node="b")],
        )
        store = MagicMock()

        import asyncio
        asyncio.run(
            _finalize_execution(engine, dag, store, "test-session", {}, None)
        )

        out = capsys.readouterr().out
        assert "[FAILED] b: import smoke test failed" in out
        assert "[SKIPPED] c: dependency b failed" in out

    def test_stderr_message_when_failures(self, capsys):
        from cli.execution import _finalize_execution

        engine = MagicMock()
        engine.get_execution_summary.return_value = {
            "total_nodes": 1, "success": 0, "failed": 1,
            "skipped": 0, "implementation_success": 0,
            "implementation_total": 0,
        }
        dag = DAG(
            nodes={"a": _make_node("a", NodeStatus.FAILED, "timeout")},
            edges=[],
        )
        store = MagicMock()

        import asyncio
        asyncio.run(
            _finalize_execution(engine, dag, store, "s1", {}, None)
        )

        assert "1 node(s) failed" in capsys.readouterr().err
