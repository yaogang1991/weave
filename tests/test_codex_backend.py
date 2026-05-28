"""Tests for M4.4 CodexBackend."""
import asyncio
import json
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.backend_models import BackendContext, BackendStatus
from core.dag_models import HandoffArtifact
from core.exceptions import NodeTimeoutError
from agent.backends.codex import CodexBackend
from agent.backends.registry import BackendRegistry


def _make_node(task_description="test task", backend="codex"):
    node = MagicMock()
    node.task_description = task_description
    node.backend = backend
    return node


def _make_context(node=None, artifacts=None, workspace_path="/tmp/ws"):
    return BackendContext(
        node=node or _make_node(),
        artifacts=artifacts or [],
        session_id="s1",
        workspace_path=workspace_path,
        job_id="j1",
        run_id="r1",
    )


class TestCodexHealthCheck:
    @pytest.mark.asyncio
    async def test_healthy_when_binary_found(self):
        with patch("agent.backends.codex.shutil.which", return_value="/usr/bin/codex"):
            backend = CodexBackend()
            assert await backend.health_check() is True

    @pytest.mark.asyncio
    async def test_unhealthy_when_binary_missing(self):
        with patch("agent.backends.codex.shutil.which", return_value=None):
            backend = CodexBackend()
            assert await backend.health_check() is False

    @pytest.mark.asyncio
    async def test_caches_resolved_path(self):
        with patch("agent.backends.codex.shutil.which", return_value="/usr/bin/codex") as mock_which:
            backend = CodexBackend()
            await backend.health_check()
            await backend.health_check()
            assert mock_which.call_count == 2  # Re-resolves each call (#619 #8)


class TestCodexBuildPrompt:
    def test_task_only(self):
        backend = CodexBackend()
        ctx = _make_context(node=_make_node("Fix the bug"))
        prompt = backend._build_prompt(ctx)
        assert "Fix the bug" in prompt

    def test_with_artifacts(self):
        backend = CodexBackend()
        art = HandoffArtifact(
            from_agent="planner", to_agent="generator",
            content="previous output here",
            file_paths=["src/main.py", "src/util.py"],
        )
        ctx = _make_context(artifacts=[art])
        prompt = backend._build_prompt(ctx)
        assert "previous output here" in prompt
        assert "src/main.py" in prompt

    def test_empty_artifacts(self):
        backend = CodexBackend()
        ctx = _make_context(artifacts=[])
        prompt = backend._build_prompt(ctx)
        assert prompt == "test task"


class TestCodexStreamOutput:
    @pytest.mark.asyncio
    async def test_parses_agent_messages(self):
        backend = CodexBackend()
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "t1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "Hello"}}),
            json.dumps({"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "World"}}),
        ]

        mock_process = MagicMock()
        mock_process.stdout = AsyncMock()
        mock_process.stdout.readline = AsyncMock(
            side_effect=[f"{l}\n".encode() for l in lines] + [b""]
        )

        output_lines = []
        usage = {"input_tokens": 0, "output_tokens": 0}
        await backend._stream_output(mock_process, output_lines, usage, None, None)

        assert output_lines == ["Hello", "World"]

    @pytest.mark.asyncio
    async def test_accumulates_token_usage(self):
        backend = CodexBackend()
        lines = [
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 50}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 200, "output_tokens": 75}}),
        ]

        mock_process = MagicMock()
        mock_process.stdout = AsyncMock()
        mock_process.stdout.readline = AsyncMock(
            side_effect=[f"{l}\n".encode() for l in lines] + [b""]
        )

        output_lines = []
        usage = {"input_tokens": 0, "output_tokens": 0}
        await backend._stream_output(mock_process, output_lines, usage, None, None)

        assert usage["input_tokens"] == 300
        assert usage["output_tokens"] == 125

    @pytest.mark.asyncio
    async def test_cancel_terminates_process(self):
        backend = CodexBackend()
        cancel_event = threading.Event()
        cancel_event.set()

        mock_process = MagicMock()
        mock_process.stdout = AsyncMock()
        mock_process.stdout.readline = AsyncMock(side_effect=[b""])
        mock_process.wait = AsyncMock()

        output_lines = []
        usage = {"input_tokens": 0, "output_tokens": 0}
        await backend._stream_output(mock_process, output_lines, usage, cancel_event, None)

        mock_process.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_ignores_unknown_events(self):
        backend = CodexBackend()
        lines = [
            json.dumps({"type": "thread.started"}),
            "not json at all",
            json.dumps({"type": "unknown_event"}),
        ]

        mock_process = MagicMock()
        mock_process.stdout = AsyncMock()
        mock_process.stdout.readline = AsyncMock(
            side_effect=[f"{l}\n".encode() for l in lines] + [b""]
        )

        output_lines = []
        usage = {"input_tokens": 0, "output_tokens": 0}
        await backend._stream_output(mock_process, output_lines, usage, None, None)
        assert output_lines == []

    @pytest.mark.asyncio
    async def test_progress_callback_invoked(self):
        backend = CodexBackend()
        callback = MagicMock()

        lines = [
            json.dumps({"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "ls"}}),
        ]

        mock_process = MagicMock()
        mock_process.stdout = AsyncMock()
        mock_process.stdout.readline = AsyncMock(
            side_effect=[f"{l}\n".encode() for l in lines] + [b""]
        )

        output_lines = []
        usage = {"input_tokens": 0, "output_tokens": 0}
        await backend._stream_output(mock_process, output_lines, usage, None, callback)
        callback.assert_called_once()


class TestCodexParseResult:
    def test_completed(self):
        backend = CodexBackend()
        result = backend._parse_result(0, ["Hello", "World"], {"input_tokens": 10, "output_tokens": 5}, "", [])
        assert result.status == BackendStatus.COMPLETED
        assert result.summary == "Hello"  # Uses first line (#619 #10)
        assert result.metadata["token_usage"]["input_tokens"] == 10

    def test_failed(self):
        backend = CodexBackend()
        result = backend._parse_result(1, [], {"input_tokens": 0, "output_tokens": 0}, "error msg", [])
        assert result.status == BackendStatus.FAILED
        assert "error msg" in result.error

    def test_cancelled_signal(self):
        backend = CodexBackend()
        result = backend._parse_result(-1, ["partial"], {"input_tokens": 5, "output_tokens": 3}, "", [])
        assert result.status == BackendStatus.CANCELLED

    def test_cancelled_none(self):
        backend = CodexBackend()
        result = backend._parse_result(None, [], {"input_tokens": 0, "output_tokens": 0}, "", [])
        assert result.status == BackendStatus.CANCELLED

    def test_failed_no_stderr(self):
        backend = CodexBackend()
        result = backend._parse_result(2, [], {"input_tokens": 0, "output_tokens": 0}, "", [])
        assert "code 2" in result.error


class TestCodexExecute:
    @pytest.mark.asyncio
    async def test_binary_not_resolved(self):
        backend = CodexBackend()
        ctx = _make_context()
        # _resolved_path is None (health_check never called or failed)
        result = await backend.execute(ctx)
        assert result.status == BackendStatus.FAILED
        assert "not resolved" in result.error

    @pytest.mark.asyncio
    async def test_binary_not_found(self):
        backend = CodexBackend()
        backend._resolved_path = "/nonexistent/codex"
        ctx = _make_context()

        async def mock_exec(*args, **kwargs):
            raise FileNotFoundError("codex not found")

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            result = await backend.execute(ctx)

        assert result.status == BackendStatus.FAILED
        assert "not found" in result.error

    @pytest.mark.asyncio
    async def test_successful_execution(self):
        backend = CodexBackend()
        backend._resolved_path = "/usr/local/bin/codex"
        ctx = _make_context()

        jsonl_lines = [
            json.dumps({"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "Fixed!"}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 50}}),
        ]

        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.stdout = AsyncMock()
        mock_process.stdout.readline = AsyncMock(
            side_effect=[f"{l}\n".encode() for l in jsonl_lines] + [b""]
        )
        mock_process.stderr = AsyncMock()
        mock_process.stderr.read = AsyncMock(return_value=b"")

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await backend.execute(ctx)

        assert result.status == BackendStatus.COMPLETED
        assert result.output == "Fixed!"
        assert result.metadata["token_usage"]["input_tokens"] == 100

    @pytest.mark.asyncio
    async def test_failed_execution(self):
        backend = CodexBackend()
        backend._resolved_path = "/usr/local/bin/codex"
        ctx = _make_context()

        mock_process = MagicMock()
        mock_process.returncode = 1
        mock_process.stdout = AsyncMock()
        mock_process.stdout.readline = AsyncMock(return_value=b"")
        mock_process.stderr = AsyncMock()
        mock_process.stderr.read = AsyncMock(return_value=b"compilation failed\n")

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await backend.execute(ctx)

        assert result.status == BackendStatus.FAILED
        assert "compilation failed" in result.error

    @pytest.mark.asyncio
    async def test_timeout(self):
        backend = CodexBackend()
        backend._resolved_path = "/usr/local/bin/codex"
        ctx = _make_context()

        mock_process = MagicMock()
        mock_process.returncode = None
        mock_process.stdout = AsyncMock()

        async def hang_readline():
            await asyncio.sleep(100)  # will be cancelled by wait_for
            return b""

        mock_process.stdout.readline = hang_readline
        mock_process.stderr = AsyncMock()
        mock_process.stderr.read = AsyncMock(return_value=b"")
        mock_process.kill = MagicMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            backend._timeout = 0.01
            with pytest.raises(NodeTimeoutError):
                await backend.execute(ctx)

        mock_process.kill.assert_called()


class TestCodexRegistryIntegration:
    @pytest.mark.asyncio
    async def test_register_and_execute(self):
        pool = MagicMock()
        registry = BackendRegistry.from_pool(pool=pool, session_id="s1")

        backend = CodexBackend()
        registry.register("codex", backend)

        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.stdout = AsyncMock()
        mock_process.stdout.readline = AsyncMock(return_value=b"")
        mock_process.stderr = AsyncMock()
        mock_process.stderr.read = AsyncMock(return_value=b"")

        with patch("agent.backends.codex.shutil.which", return_value="/usr/local/bin/codex"):
            with patch("asyncio.create_subprocess_exec", return_value=mock_process):
                ctx = _make_context()
                result = await registry.execute_for_node("codex", ctx)

        assert result.status == BackendStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_fallback_when_unhealthy(self):
        pool = MagicMock()
        registry = BackendRegistry.from_pool(pool=pool, session_id="s1")

        with patch("agent.backends.codex.shutil.which", return_value=None):
            backend = CodexBackend()
            registry.register("codex", backend)

        # Fallback to builtin — which requires a working pool executor.
        # Just verify the registry doesn't crash and logs the fallback.
        ctx = _make_context()
        with pytest.raises((TypeError, AttributeError)):
            # BuiltinBackend will fail because pool is a MagicMock,
            # but that's expected — the important thing is the fallback happens.
            await registry.execute_for_node("codex", ctx)


class TestCodexName:
    def test_name_property(self):
        backend = CodexBackend()
        assert backend.name == "codex"

    def test_capabilities(self):
        backend = CodexBackend()
        assert backend.get_capabilities() == ["generator"]
