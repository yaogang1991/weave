"""Tests for #1135: --timeout flag enforced on run/execute.

Previously the CLI --timeout flag was parsed for run/execute but never
applied — _execute_with_error_handling called engine.execute(dag) with
no asyncio.wait_for envelope, and cmd_run dropped the timeout field
when building exec_args. Now the run-level wall-clock ceiling
(args.timeout or config.run_timeout_sec) is enforced.
"""
import argparse
import asyncio

import pytest

from cli import execution as exe
from cli.execution import _execute_with_error_handling
from core.dag_models import DAG, DAGNode
from core.models import EventType


class _FakeStore:
    def __init__(self):
        self.events = []

    def emit_event(self, session_id, event_type, details):
        self.events.append((session_id, event_type, details))


class _FakeEngine:
    """Engine whose execute() sleeps for ``delay`` then returns the dag.

    Records whether it was cancelled so we can assert the timeout envelope
    actually interrupted it.
    """

    def __init__(self, delay: float = 0.0) -> None:
        self._delay = delay
        self.was_cancelled = False
        self.started = False

    async def execute(self, dag):
        self.started = True
        try:
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            self.was_cancelled = True
            raise
        return dag


def _dag() -> DAG:
    d = DAG(reasoning="t")
    return d.add_node(DAGNode(id="n1", agent_type="generator", task_description="t"))


class TestExecuteTimeoutEnforced:
    """#1135: timeout envelope around engine.execute."""

    @pytest.mark.asyncio
    async def test_timeout_aborts_long_run(self):
        engine = _FakeEngine(delay=10.0)
        store = _FakeStore()

        result = await _execute_with_error_handling(
            engine, _dag(), store, "sess", timeout=0.05,
        )

        assert result is None
        assert engine.was_cancelled
        error_events = [e for e in store.events if e[1] == EventType.SESSION_ERROR]
        assert len(error_events) == 1
        assert "wall-clock timeout" in error_events[0][2]["error"]

    @pytest.mark.asyncio
    async def test_no_timeout_runs_to_completion(self):
        engine = _FakeEngine(delay=0.0)
        store = _FakeStore()
        dag = _dag()

        result = await _execute_with_error_handling(
            engine, dag, store, "sess", timeout=None,
        )

        assert result is dag
        assert not engine.was_cancelled
        assert store.events == []

    @pytest.mark.asyncio
    async def test_timeout_zero_treated_as_no_limit(self):
        """timeout=0 (falsy) must NOT wrap — backward compat."""
        engine = _FakeEngine(delay=0.0)
        store = _FakeStore()
        dag = _dag()

        result = await _execute_with_error_handling(
            engine, dag, store, "sess", timeout=0,
        )

        assert result is dag
        assert not engine.was_cancelled

    @pytest.mark.asyncio
    async def test_completion_within_timeout_succeeds(self):
        engine = _FakeEngine(delay=0.01)
        store = _FakeStore()
        dag = _dag()

        result = await _execute_with_error_handling(
            engine, dag, store, "sess", timeout=5.0,
        )

        assert result is dag
        assert not engine.was_cancelled


class TestCmdRunPropagatesTimeout:
    """#1135 root cause 1: cmd_run must forward --timeout to cmd_execute."""

    @pytest.mark.asyncio
    async def test_cmd_run_forwards_timeout(self, monkeypatch, tmp_path):
        captured: dict = {}

        async def fake_cmd_plan(args):
            return _dag()

        async def fake_cmd_execute(args, dag=None):
            captured["timeout"] = getattr(args, "timeout", "MISSING")
            return dag

        monkeypatch.setattr(exe, "cmd_plan", fake_cmd_plan)
        monkeypatch.setattr(exe, "cmd_execute", fake_cmd_execute)
        monkeypatch.setattr(exe, "_check_dirty_workspace", lambda *a, **k: None)
        monkeypatch.setattr(exe, "_check_stdlib_shadowing", lambda *a, **k: None)

        args = argparse.Namespace(
            file=None,
            requirement="do something",
            project=str(tmp_path),
            max_parallel=1,
            max_iterations=1,
            non_interactive=True,
            allow_self_modify=False,
            viz=False,
            visualize=False,
            no_browser=True,
            timeout=42,
        )

        await exe.cmd_run(args)

        assert captured["timeout"] == 42, (
            "cmd_run must propagate --timeout (42) into cmd_execute's args"
        )
