"""Unit tests for agent/worker.py — AgentWorker core loop.

Covers: tool call validation, artifact tracking, context truncation,
stuck detection integration, memory persistence, cancellation, token usage.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from core.config import LLMConfig
from core.models import AgentMessage, EventType, ToolResult
from agent.worker import (
    AgentWorker,
    TOOL_REQUIRED_ARGS,
    TOOL_NON_EMPTY_ARGS,
    EMPTY_CALL_MAX_RETRIES,
)


# -- Helpers ---------------------------------------------------------------

def _make_config() -> LLMConfig:
    return LLMConfig(
        api_key="test-key",
        model="test-model",
        provider="anthropic",
    )


def _make_worker(**overrides) -> AgentWorker:
    store = MagicMock()
    cfg = _make_config()
    with patch("agent.worker.LLMClient"):
        worker = AgentWorker(config=cfg, session_store=store, **overrides)
    return worker


def _tool_response(tool_calls: list[dict], content: str = "") -> dict:
    return {
        "content": content,
        "tool_calls": tool_calls,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def _text_response(content: str = "done") -> dict:
    return {
        "content": content,
        "tool_calls": [],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def _tool_call(name: str, arguments: dict | None = None, call_id: str = "tc_1") -> dict:
    return {
        "id": call_id,
        "name": name,
        "arguments": arguments if arguments is not None else {},
    }


def _mock_tool_executor(results: dict[str, ToolResult] | None = None):
    default_results = results or {}
    executor = MagicMock()

    def _execute(name, args):
        if name in default_results:
            return default_results[name]
        return ToolResult(tool_call_id="", success=True, output=f"{name} ok", error="")

    executor.execute.side_effect = _execute
    return executor


# -- Test Classes -----------------------------------------------------------

class TestToolCallValidation:
    """_execute_tool_calls validates args before execution."""

    def test_missing_required_args_skips_execution(self):
        worker = _make_worker()
        executor = _mock_tool_executor()
        msg = _tool_response([_tool_call("write", {"content": "hi"})])

        results, any_exec = worker._execute_tool_calls(msg, "sess", executor)

        assert any_exec is False
        assert len(results) == 1
        assert "missing required argument" in results[0]["content"].lower()
        executor.execute.assert_not_called()

    def test_blank_non_empty_args_skips_execution(self):
        worker = _make_worker()
        executor = _mock_tool_executor()
        msg = _tool_response([_tool_call("bash", {"command": "  "})])

        results, any_exec = worker._execute_tool_calls(msg, "sess", executor)

        assert any_exec is False
        assert len(results) == 1
        assert "must not be empty" in results[0]["content"].lower()

    def test_valid_args_executed(self):
        worker = _make_worker()
        executor = _mock_tool_executor()
        msg = _tool_response([_tool_call("read", {"file_path": "/tmp/x.py"})])

        results, any_exec = worker._execute_tool_calls(msg, "sess", executor)

        assert any_exec is True
        assert len(results) == 1
        assert results[0]["role"] == "tool"

    def test_mixed_valid_invalid_executes_valid_only(self):
        worker = _make_worker()
        executor = _mock_tool_executor()
        msg = _tool_response([
            _tool_call("write", {"content": "x"}, call_id="bad"),
            _tool_call("read", {"file_path": "/tmp/a.py"}, call_id="good"),
        ])

        results, any_exec = worker._execute_tool_calls(msg, "sess", executor)

        assert any_exec is True
        assert len(results) == 2
        assert "missing required" in results[0]["content"].lower()
        assert results[1]["content"] == "read ok"

    def test_missing_tool_call_id_gets_generated(self):
        worker = _make_worker()
        executor = _mock_tool_executor()
        msg = _tool_response([{"name": "read", "arguments": {"file_path": "/t.py"}, "id": ""}])

        results, any_exec = worker._execute_tool_calls(msg, "sess", executor)

        assert any_exec is True
        assert results[0]["tool_call_id"].startswith("tool_")

    def test_tool_failure_still_records_result(self):
        worker = _make_worker()
        executor = _mock_tool_executor({
            "read": ToolResult(tool_call_id="", success=False, output="", error="file not found"),
        })
        msg = _tool_response([_tool_call("read", {"file_path": "/missing.py"})])

        results, any_exec = worker._execute_tool_calls(msg, "sess", executor)

        assert any_exec is True
        assert "file not found" in results[0]["content"]


class TestArtifactTracking:
    """_track_artifact records file paths from write/edit calls."""

    def test_write_tracks_existing_file(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("print('hello')")
        worker = _make_worker()
        worker._base_cwd = tmp_path

        worker._track_artifact("write", {"file_path": str(f)})

        assert str(f) in worker.artifacts

    def test_write_skips_nonexistent_file(self, tmp_path):
        worker = _make_worker()
        worker._track_artifact("write", {"file_path": str(tmp_path / "nope.py")})
        assert worker.artifacts == []

    def test_write_skips_empty_file(self, tmp_path):
        f = tmp_path / "empty.py"
        f.write_text("")
        worker = _make_worker()
        worker._track_artifact("write", {"file_path": str(f)})
        assert worker.artifacts == []

    def test_edit_tracks_existing_file(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("x = 1")
        worker = _make_worker()
        worker._base_cwd = tmp_path

        worker._track_artifact("edit", {"file_path": str(f)})
        assert str(f) in worker.artifacts

    def test_non_write_edit_ignored(self, tmp_path):
        worker = _make_worker()
        worker._track_artifact("bash", {"command": "ls"})
        assert worker.artifacts == []

    def test_no_duplicate_entries(self, tmp_path):
        f = tmp_path / "dup.py"
        f.write_text("x = 1")
        worker = _make_worker()
        worker._base_cwd = tmp_path

        worker._track_artifact("write", {"file_path": str(f)})
        worker._track_artifact("edit", {"file_path": str(f)})

        assert worker.artifacts.count(str(f)) == 1


class TestContextTruncation:
    """_truncate_messages keeps system prompt + tail."""

    def test_short_messages_unchanged(self):
        worker = _make_worker()
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
        assert worker._truncate_messages(msgs, 10000) is msgs

    def test_truncation_preserves_system_prompt(self):
        worker = _make_worker()
        msgs = [{"role": "system", "content": "sys"}]
        msgs += [{"role": "user", "content": f"msg{i}" * 50} for i in range(50)]

        result = worker._truncate_messages(msgs, 200)

        assert result[0]["role"] == "system"
        assert result[0]["content"] == "sys"
        assert len(result) < len(msgs)

    def test_truncation_keeps_tail(self):
        worker = _make_worker()
        msgs = [{"role": "system", "content": "sys"}]
        msgs += [{"role": "user", "content": f"msg{i}"} for i in range(50)]

        result = worker._truncate_messages(msgs, 100)

        assert result[-1]["content"] == "msg49"

    def test_estimate_tokens_positive(self):
        worker = _make_worker()
        tokens = worker._estimate_tokens([{"role": "user", "content": "hello"}])
        assert tokens >= 1

    def test_estimate_tokens_cjk(self):
        worker = _make_worker()
        tokens = worker._estimate_tokens([{"role": "user", "content": "你好世界"}])
        assert tokens >= 1


class TestRunLoop:
    """AgentWorker.run() main loop behaviour."""

    def test_text_response_terminates_loop(self):
        worker = _make_worker()
        worker.llm.call = MagicMock(return_value=_text_response("all done"))
        executor = _mock_tool_executor()

        msgs = list(worker.run("sess", "sys", "do task", [], executor, max_iterations=5))

        assert len(msgs) == 1
        assert msgs[0].content == "all done"

    def test_tool_then_text_two_iterations(self):
        worker = _make_worker()
        executor = _mock_tool_executor()
        worker.llm.call = MagicMock(side_effect=[
            _tool_response([_tool_call("read", {"file_path": "/tmp/x.py"})]),
            _text_response("finished"),
        ])

        msgs = list(worker.run("sess", "sys", "read file", [], executor, max_iterations=5))

        assert len(msgs) == 2
        assert msgs[0].tool_calls is not None and len(msgs[0].tool_calls) == 1
        assert msgs[1].content == "finished"

    def test_max_iterations_limits_loop(self):
        worker = _make_worker()
        worker.llm.call = MagicMock(return_value=_tool_response(
            [_tool_call("read", {"file_path": "/tmp/x.py"})],
        ))
        executor = _mock_tool_executor()

        msgs = list(worker.run("sess", "sys", "loop", [], executor, max_iterations=3))

        assert len(msgs) == 3

    def test_cancel_event_stops_loop(self):
        worker = _make_worker()
        executor = _mock_tool_executor()
        call_count = 0

        def _call(messages, tools, cancel_event=None, **kw):
            nonlocal call_count
            call_count += 1
            if cancel_event:
                cancel_event.set()
            return _tool_response([_tool_call("read", {"file_path": "/tmp/x.py"})])

        worker.llm.call = _call
        cancel = threading.Event()

        msgs = list(worker.run(
            "sess", "sys", "cancel test", [], executor,
            max_iterations=10, cancel_event=cancel,
        ))

        assert call_count == 1
        assert len(msgs) == 1

    def test_artifacts_reset_on_new_run(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1")
        worker = _make_worker()
        worker._base_cwd = tmp_path

        executor = _mock_tool_executor()
        worker.llm.call = MagicMock(side_effect=[
            _tool_response([_tool_call("write", {"file_path": str(f), "content": "x"})]),
            _text_response("done"),
        ])
        list(worker.run("sess1", "sys", "write file", [], executor, max_iterations=5))
        assert str(f) in worker.artifacts

        worker.llm.call = MagicMock(return_value=_text_response("done"))
        list(worker.run("sess2", "sys", "do nothing", [], executor, max_iterations=5))
        assert worker.artifacts == []

    def test_token_usage_accumulated(self):
        worker = _make_worker()
        worker.llm.call = MagicMock(return_value=_tool_response(
            [_tool_call("read", {"file_path": "/tmp/x.py"})],
        ))
        executor = _mock_tool_executor()

        # Use max_iterations to force loop completion (last_token_usage set after loop)
        list(worker.run("sess", "sys", "task", [], executor, max_iterations=2))

        # _run_token_usage accumulates during loop; last_token_usage set after loop ends
        assert worker._run_token_usage["input_tokens"] > 0


class TestEmptyToolCallRetry:
    """Auto-retry for empty/invalid tool call args (#282)."""

    def test_retries_on_missing_args_then_succeeds(self):
        worker = _make_worker()
        executor = _mock_tool_executor()
        worker.llm.call = MagicMock(side_effect=[
            _tool_response([_tool_call("write", {"content": "x"})]),
            _tool_response([_tool_call("write", {"file_path": "/t.py", "content": "x"})]),
            _text_response("done"),
        ])

        msgs = list(worker.run("sess", "sys", "write file", [], executor, max_iterations=5))

        assert len(msgs) == 2

    def test_all_empty_args_skips_retries(self):
        worker = _make_worker()
        executor = _mock_tool_executor()
        worker.llm.call = MagicMock(return_value=_tool_response([
            _tool_call("write", {}),
            _tool_call("read", {}),
        ]))

        msgs = list(worker.run("sess", "sys", "stuck", [], executor, max_iterations=20))

        assert worker.llm.call.call_count <= 4


class TestToolArgConstants:
    """Verify TOOL_REQUIRED_ARGS / TOOL_NON_EMPTY_ARGS consistency."""

    def test_required_args_keys_are_known_tools(self):
        known = {"write", "edit", "read", "bash", "glob", "grep"}
        assert set(TOOL_REQUIRED_ARGS.keys()) == known

    def test_non_empty_args_subset_of_required(self):
        for tool, fields in TOOL_NON_EMPTY_ARGS.items():
            assert tool in TOOL_REQUIRED_ARGS
            for f in fields:
                assert f in TOOL_REQUIRED_ARGS[tool]


class TestMemoryPersistence:
    """_persist_memory skips gracefully when no memory manager."""

    def test_no_memory_manager(self):
        worker = _make_worker()
        worker._persist_memory("sess", "task", [{"role": "user", "content": "hi"}])

    def test_disabled_memory_manager(self):
        mm = MagicMock()
        mm.config.enabled = False
        worker = _make_worker(memory_manager=mm)
        worker._persist_memory("sess", "task", [])
        mm.extract_and_store.assert_not_called()

    def test_enabled_auto_store_calls_extract(self):
        mm = MagicMock()
        mm.config.enabled = True
        mm.config.auto_store = True
        worker = _make_worker(memory_manager=mm)
        worker.artifacts = ["a.py"]

        worker._persist_memory("sess", "task", [{"role": "user", "content": "hi"}])

        mm.extract_and_store.assert_called_once()

    def test_extract_failure_does_not_raise(self):
        mm = MagicMock()
        mm.config.enabled = True
        mm.config.auto_store = True
        mm.extract_and_store.side_effect = RuntimeError("boom")
        worker = _make_worker(memory_manager=mm)
        worker._persist_memory("sess", "task", [])


class TestEventEmission:
    """Session store receives correct events during execution."""

    def test_tool_use_event_emitted(self):
        worker = _make_worker()
        executor = _mock_tool_executor()
        msg = _tool_response([_tool_call("read", {"file_path": "/tmp/x.py"}, call_id="tc_42")])

        worker._execute_tool_calls(msg, "sess", executor)

        tool_use_calls = [
            c for c in worker.session_store.emit_event.call_args_list
            if c[0][1] == EventType.AGENT_TOOL_USE
        ]
        assert len(tool_use_calls) == 1
        assert tool_use_calls[0][0][2]["name"] == "read"

    def test_tool_result_event_emitted_on_success(self):
        worker = _make_worker()
        executor = _mock_tool_executor()
        msg = _tool_response([_tool_call("read", {"file_path": "/tmp/x.py"})])

        worker._execute_tool_calls(msg, "sess", executor)

        result_calls = [
            c for c in worker.session_store.emit_event.call_args_list
            if c[0][1] == EventType.AGENT_TOOL_RESULT
        ]
        assert len(result_calls) == 1

    def test_tool_result_event_emitted_on_validation_failure(self):
        worker = _make_worker()
        executor = _mock_tool_executor()
        msg = _tool_response([_tool_call("write", {"content": "x"})])

        worker._execute_tool_calls(msg, "sess", executor)

        result_calls = [
            c for c in worker.session_store.emit_event.call_args_list
            if c[0][1] == EventType.AGENT_TOOL_RESULT
        ]
        assert len(result_calls) == 1
        assert result_calls[0][0][2]["success"] is False

    def test_agent_message_event_on_text_response(self):
        worker = _make_worker()
        worker.llm.call = MagicMock(return_value=_text_response("hello"))
        executor = _mock_tool_executor()

        list(worker.run("sess", "sys", "task", [], executor, max_iterations=5))

        msg_calls = [
            c for c in worker.session_store.emit_event.call_args_list
            if c[0][1] == EventType.AGENT_MESSAGE
        ]
        assert len(msg_calls) >= 1


class TestStuckDetection:
    """Circuit breaker and degenerate detection terminate stuck loops."""

    def test_degenerate_empty_args_triggers_stuck_event(self):
        worker = _make_worker()
        executor = _mock_tool_executor()
        # Always returns empty-args tool calls — degenerate pattern
        worker.llm.call = MagicMock(return_value=_tool_response([
            _tool_call("write", {}),
        ]))

        msgs = list(worker.run("sess", "sys", "stuck", [], executor, max_iterations=20))

        stuck_events = [
            c for c in worker.session_store.emit_event.call_args_list
            if c[0][1] == EventType.AGENT_STUCK
        ]
        assert len(stuck_events) >= 1

    def test_stuck_loop_stops_before_max_iterations(self):
        worker = _make_worker()
        executor = _mock_tool_executor()
        worker.llm.call = MagicMock(return_value=_tool_response([
            _tool_call("write", {}),
        ]))

        msgs = list(worker.run("sess", "sys", "stuck", [], executor, max_iterations=100))

        # DEGENERATE_CALL_LIMIT=3, plus hint injections, should stop well before 100
        assert len(msgs) < 20

    def test_recovery_hint_injected_on_degenerate(self):
        worker = _make_worker()
        executor = _mock_tool_executor()
        call_count = 0
        original_call = worker.llm.call

        def _call(messages, tools, cancel_event=None, **kw):
            nonlocal call_count
            call_count += 1
            return _tool_response([_tool_call("write", {})])

        worker.llm.call = _call
        list(worker.run("sess", "sys", "stuck", [], executor, max_iterations=20))

        # Recovery hints are injected as user messages with "CRITICAL:" prefix
        # Check that the session recorded degenerate detection
        stuck_events = [
            c for c in worker.session_store.emit_event.call_args_list
            if c[0][1] == EventType.AGENT_STUCK
        ]
        assert any("degenerate" in str(c).lower() for c in stuck_events)


class TestOutputMonitoring:
    """Output injection scanning in _execute_tool_calls."""

    def test_injection_detected_sanitizes_output(self):
        worker = _make_worker()
        monitor = MagicMock()
        monitor.scan_tool_output.return_value = MagicMock(
            injected=True,
            sanitized_output="CLEANED",
            risk_level="high",
            patterns_matched=["ignore_previous"],
        )
        worker._output_monitor = monitor

        executor = _mock_tool_executor()
        msg = _tool_response([_tool_call("read", {"file_path": "/tmp/x.py"})])

        results, any_exec = worker._execute_tool_calls(msg, "sess", executor)

        assert any_exec is True
        # Output should be sanitized
        assert results[0]["content"] == "CLEANED"

        # AGENT_ERROR event emitted
        error_events = [
            c for c in worker.session_store.emit_event.call_args_list
            if c[0][1] == EventType.AGENT_ERROR
        ]
        assert len(error_events) == 1
        assert error_events[0][0][2]["error"] == "output_injection_detected"

    def test_clean_output_passes_through(self):
        worker = _make_worker()
        monitor = MagicMock()
        monitor.scan_tool_output.return_value = MagicMock(
            injected=False,
            sanitized_output="",
            risk_level="low",
            patterns_matched=[],
        )
        worker._output_monitor = monitor

        executor = _mock_tool_executor()
        msg = _tool_response([_tool_call("read", {"file_path": "/tmp/x.py"})])

        results, any_exec = worker._execute_tool_calls(msg, "sess", executor)

        assert any_exec is True
        # Original output preserved (not sanitized)
        assert results[0]["content"] == "read ok"
