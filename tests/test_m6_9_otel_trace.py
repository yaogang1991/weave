"""Tests for M6.9 (#964): OTEL trace propagation to CLI subprocess."""
import asyncio
import json
from unittest.mock import MagicMock, patch


from core.backend_models import BackendContext, BackendStatus
from core.dag_models import DAGNode
from monitoring.otel import (
    NoOpSpan,
    inject_trace_context,
    start_backend_call_span,
)


def _make_node(agent_type: str = "generator", task: str = "test task") -> DAGNode:
    return DAGNode(
        id="node_1",
        agent_type=agent_type,
        task_description=task,
    )


def _make_context(
    node: DAGNode | None = None,
    run_id: str | None = None,
) -> BackendContext:
    return BackendContext(
        node=node or _make_node(),
        session_id="sess_1",
        workspace_path="/tmp/test",
        job_id="job_1",
        run_id=run_id,
    )


class TestInjectTraceContext:
    """Tests for inject_trace_context helper."""

    def test_returns_env_unchanged_when_no_span_active(self):
        env = {"PATH": "/usr/bin", "HOME": "/home/test"}
        result = inject_trace_context(env)
        assert result == env
        assert "TRACEPARENT" not in result

    def test_does_not_mutate_original(self):
        original = {"PATH": "/usr/bin"}
        result = inject_trace_context(original)
        # When no trace active, the same dict is returned (no mutation needed).
        assert "PATH" in result
        assert "PATH" in original

    def test_preserves_all_existing_keys(self):
        env = {"A": "1", "B": "2", "C": "3"}
        result = inject_trace_context(env)
        assert result["A"] == "1"
        assert result["B"] == "2"
        assert result["C"] == "3"

    def test_injects_traceparent_when_span_active(self):
        """When an active OTel span exists, traceparent should be injected."""
        trace_ctx = {"TRACEPARENT": "00-abcdef1234567890abcdef1234567890-1234567890abcdef-01"}
        env = {"PATH": "/usr/bin"}

        with patch("monitoring.otel.get_trace_context", return_value=trace_ctx):
            result = inject_trace_context(env)

        assert "TRACEPARENT" in result
        assert result["TRACEPARENT"].startswith("00-")
        assert result["PATH"] == "/usr/bin"
        # Original should not be mutated.
        assert "TRACEPARENT" not in env


class TestStartBackendCallSpan:
    """Tests for start_backend_call_span helper."""

    def test_returns_noop_without_otel(self):
        span = start_backend_call_span("run_1", "node_1", "claude_code")
        assert isinstance(span, NoOpSpan)

    def test_span_name_and_attributes_with_tracer(self):
        """When OTel tracer is available, span has correct name and attributes."""
        from tests.test_otel_spans import _RecordingSpan

        recording = _RecordingSpan()

        with patch("monitoring.otel.get_tracer") as mock_get_tracer:
            mock_tracer = MagicMock()
            mock_tracer.start_as_current_span.return_value = recording
            mock_get_tracer.return_value = mock_tracer

            span = start_backend_call_span("run_42", "node_7", "claude_code")

            mock_tracer.start_as_current_span.assert_called_once_with(
                "backend_call claude_code",
            )
            assert span is recording
            assert span.attributes["weave.run.id"] == "run_42"
            assert span.attributes["weave.node.id"] == "node_7"
            assert span.attributes["weave.backend.name"] == "claude_code"


class TestClaudeCodeBackendTraceInjection:
    """Tests that ClaudeCodeBackend._execute_via_cli injects trace context."""

    def _mock_process(self, lines, returncode=0, stderr=b""):
        process = MagicMock()
        process.returncode = returncode
        line_iter = iter(lines)

        async def readline():
            return next(line_iter, b"")

        stdout_mock = MagicMock()
        stdout_mock.readline = readline
        process.stdout = stdout_mock

        stderr_mock = MagicMock()

        async def read_stderr():
            return stderr

        stderr_mock.read = read_stderr
        process.stderr = stderr_mock
        process.terminate = MagicMock()
        process.kill = MagicMock()
        return process

    def test_execute_via_cli_passes_env_with_trace_context(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig

        backend = ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())
        backend._sdk_available = False
        ctx = _make_context(run_id="run_otel_test")

        lines = [
            json.dumps({
                "type": "result",
                "result": "done",
                "is_error": False,
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "session_id": "sess_1",
            }).encode() + b"\n",
        ]
        mock_proc = self._mock_process(lines, returncode=0)

        captured_env = {}

        async def capture_subprocess_exec(*args, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=capture_subprocess_exec):
            result = asyncio.run(
                backend._execute_via_cli(ctx, "test prompt"),
            )

        assert result.status == BackendStatus.COMPLETED
        # env must have been passed to subprocess
        assert len(captured_env) > 0
        assert "PATH" in captured_env

    def test_execute_via_cli_wraps_in_backend_call_span(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig

        backend = ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())
        backend._sdk_available = False
        ctx = _make_context(run_id="run_span_test")

        lines = [
            json.dumps({
                "type": "result",
                "result": "ok",
                "is_error": False,
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "session_id": "sess_1",
            }).encode() + b"\n",
        ]
        mock_proc = self._mock_process(lines, returncode=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch(
                "agent.backends.claude_code.start_backend_call_span",
            ) as mock_span_fn:
                mock_span_fn.return_value = NoOpSpan()
                result = asyncio.run(
                    backend._execute_via_cli(ctx, "test prompt"),
                )

        mock_span_fn.assert_called_once_with(
            "run_span_test", "node_1", "claude_code",
        )
        assert result.status == BackendStatus.COMPLETED


class TestCodexBackendTraceInjection:
    """Tests that CodexBackend.execute injects trace context."""

    def _mock_process(self, lines, returncode=0, stderr=b""):
        process = MagicMock()
        process.returncode = returncode
        line_iter = iter(lines)

        async def readline():
            return next(line_iter, b"")

        stdout_mock = MagicMock()
        stdout_mock.readline = readline
        process.stdout = stdout_mock

        stderr_mock = MagicMock()

        async def read_stderr():
            return stderr

        stderr_mock.read = read_stderr
        process.stderr = stderr_mock
        process.terminate = MagicMock()
        process.kill = MagicMock()
        return process

    def test_execute_passes_env_with_trace_context(self):
        from agent.backends.codex import CodexBackend

        backend = CodexBackend()
        backend._resolved_path = "/usr/local/bin/codex"
        ctx = _make_context(run_id="run_codex_otel")

        lines = [
            json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "done"},
            }).encode() + b"\n",
        ]
        mock_proc = self._mock_process(lines, returncode=0)

        captured_env = {}

        async def capture_subprocess_exec(*args, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=capture_subprocess_exec):
            result = asyncio.run(backend.execute(ctx))

        assert result.status == BackendStatus.COMPLETED
        # env was passed to subprocess
        assert len(captured_env) > 0
        assert "PATH" in captured_env

    def test_execute_wraps_in_backend_call_span(self):
        from agent.backends.codex import CodexBackend

        backend = CodexBackend()
        backend._resolved_path = "/usr/local/bin/codex"
        ctx = _make_context(run_id="run_codex_span")

        lines = [
            json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "done"},
            }).encode() + b"\n",
        ]
        mock_proc = self._mock_process(lines, returncode=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch(
                "agent.backends.codex.start_backend_call_span",
            ) as mock_span_fn:
                mock_span_fn.return_value = NoOpSpan()
                result = asyncio.run(backend.execute(ctx))

        mock_span_fn.assert_called_once_with(
            "run_codex_span", "node_1", "codex",
        )
        assert result.status == BackendStatus.COMPLETED
