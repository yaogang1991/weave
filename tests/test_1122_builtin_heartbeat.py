"""Tests for #1122: BuiltinBackend emits heartbeats during LLM calls.

Without periodic heartbeats, long LLM calls (> stall_timeout) on the
lightweight path (planner/evaluator nodes) are force-killed by the
StallDetector before the response completes -- leaving the node dead
with input_tokens=0, output_tokens=0.

The fix: ``_execute_lightweight`` starts a periodic heartbeat task
tied to the node's ``progress_callback`` while awaiting the LLM call,
and cancels it in a ``finally`` block.
"""
from __future__ import annotations

import asyncio

import pytest

from agent.backends.builtin import BuiltinBackend
from core.backend_models import BackendContext, BackendStatus
from core.dag_models import DAGNode


class _FakeCaller:
    """Fake LightweightLLMCaller.

    Blocks for ``delay`` seconds inside ``call`` to simulate a slow LLM
    response, then returns ``response``. Exposes ``token_usage`` because
    ``_execute_lightweight`` reads it for the result metadata.
    """

    def __init__(self, delay: float = 0.0, response: str = "ok") -> None:
        self._delay = delay
        self._response = response
        self.token_usage = {"input_tokens": 10, "output_tokens": 5}
        self.call_count = 0

    async def call(self, **kwargs):
        self.call_count += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._response


class _BoomCaller:
    """Fake caller whose ``call`` always raises, to exercise the finally block."""

    token_usage: dict = {}

    async def call(self, **kwargs):
        raise RuntimeError("LLM exploded")


def _ctx(progress_callback=None, agent_type: str = "planner") -> BackendContext:
    """Build a minimal BackendContext for the lightweight path."""
    node = DAGNode(
        id="n1",
        agent_type=agent_type,
        task_description="Plan the implementation",
    )
    return BackendContext(
        node=node,
        artifacts=[],
        progress_callback=progress_callback,
    )


class TestBuiltinBackendHeartbeat:
    """#1122: heartbeats must fire during long lightweight LLM calls."""

    @pytest.mark.asyncio
    async def test_lightweight_emits_heartbeat_during_llm_call(self):
        """The progress_callback fires at least once while awaiting the LLM."""
        beats: list[int] = []
        backend = BuiltinBackend(lightweight_caller=_FakeCaller(delay=0.06))
        backend._HEARTBEAT_INTERVAL_SEC = 0.01  # fire well within the test

        result = await backend.execute(_ctx(progress_callback=lambda: beats.append(1)))

        assert result.status == BackendStatus.COMPLETED
        assert len(beats) >= 1, "at least one heartbeat should fire during the call"

    @pytest.mark.asyncio
    async def test_no_callback_backward_compat(self):
        """When progress_callback is None, execution still succeeds (no task)."""
        backend = BuiltinBackend(lightweight_caller=_FakeCaller(delay=0.01))

        result = await backend.execute(_ctx(progress_callback=None))

        assert result.status == BackendStatus.COMPLETED
        assert result.output == "ok"

    def test_start_heartbeat_returns_none_without_callback(self):
        """No callback wired -> no task created (backward compat)."""
        backend = BuiltinBackend(lightweight_caller=_FakeCaller())
        assert backend._start_heartbeat(_ctx(progress_callback=None)) is None

    @pytest.mark.asyncio
    async def test_start_heartbeat_returns_task_with_callback(self):
        """A callback is wired -> a heartbeat task is created (not None)."""
        backend = BuiltinBackend(lightweight_caller=_FakeCaller())
        task = backend._start_heartbeat(
            _ctx(progress_callback=lambda: None)
        )
        assert task is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_start_heartbeat_loop_invokes_callback(self):
        """The heartbeat task repeatedly calls the callback until cancelled."""
        backend = BuiltinBackend(lightweight_caller=_FakeCaller())
        backend._HEARTBEAT_INTERVAL_SEC = 0.01
        beats: list[int] = []
        task = backend._start_heartbeat(
            _ctx(progress_callback=lambda: beats.append(1))
        )
        assert task is not None
        try:
            await asyncio.sleep(0.035)  # ~3 intervals
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert len(beats) >= 2

    @pytest.mark.asyncio
    async def test_execute_propagates_llm_exception_and_cleans_up(self):
        """If the LLM call raises, the exception propagates AND the heartbeat
        task is cancelled (no leaked pending task)."""
        backend = BuiltinBackend(lightweight_caller=_BoomCaller())
        backend._HEARTBEAT_INTERVAL_SEC = 0.01

        with pytest.raises(RuntimeError, match="LLM exploded"):
            await backend.execute(_ctx(progress_callback=lambda: None))

        # The heartbeat task must have been cancelled in the finally block.
        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if t is not current]
        assert pending == [], "heartbeat task leaked after exception"
