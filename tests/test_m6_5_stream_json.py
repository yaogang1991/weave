"""Tests for M6.5: Stream-JSON event parsing and backend integration."""
import asyncio
import json
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock

from agent.backends.stream_parser import StreamParser
from core.backend_models import BackendContext, BackendResult, BackendStatus


def _jl(obj: dict) -> bytes:
    """Encode dict to NDJSON bytes line."""
    return json.dumps(obj).encode() + b"\n"


# ---------------------------------------------------------------------------
# StreamParser tests
# ---------------------------------------------------------------------------


class TestStreamParser(unittest.TestCase):
    """Unit tests for StreamParser."""

    def test_feed_line_assistant(self):
        parser = StreamParser()
        msg = parser.feed_line('{"type":"assistant","message":{"role":"user"}}')
        assert msg is not None
        assert msg.raw_type == "assistant"
        assert msg.data["type"] == "assistant"

    def test_feed_line_result(self):
        parser = StreamParser()
        msg = parser.feed_line('{"type":"result","result":"done","is_error":false}')
        assert msg is not None
        assert msg.raw_type == "result"
        assert msg.data["result"] == "done"

    def test_feed_line_system(self):
        parser = StreamParser()
        msg = parser.feed_line('{"type":"system","session_id":"abc123"}')
        assert msg is not None
        assert msg.raw_type == "system"

    def test_feed_line_user(self):
        parser = StreamParser()
        msg = parser.feed_line('{"type":"user","message":{}}')
        assert msg is not None
        assert msg.raw_type == "user"

    def test_feed_line_empty(self):
        parser = StreamParser()
        assert parser.feed_line("") is None
        assert parser.feed_line("   ") is None

    def test_feed_line_malformed(self):
        parser = StreamParser()
        assert parser.feed_line("not json") is None
        assert parser.feed_line("{broken") is None

    def test_feed_line_unknown_type(self):
        parser = StreamParser()
        assert parser.feed_line('{"type":"custom","data":1}') is None

    def test_feed_line_no_type(self):
        parser = StreamParser()
        assert parser.feed_line('{"message":"hello"}') is None

    def test_messages_accumulated(self):
        parser = StreamParser()
        parser.feed_line('{"type":"assistant","message":{}}')
        parser.feed_line('{"type":"result","result":"ok"}')
        assert len(parser.messages) == 2
        assert parser.messages[0].raw_type == "assistant"
        assert parser.messages[1].raw_type == "result"

    def test_messages_returns_copy(self):
        parser = StreamParser()
        parser.feed_line('{"type":"assistant","message":{}}')
        msgs = parser.messages
        msgs.clear()
        assert len(parser.messages) == 1


# ---------------------------------------------------------------------------
# BackendContext event_callback tests
# ---------------------------------------------------------------------------


class TestBackendContextEventCallback(unittest.TestCase):
    """Tests for BackendContext.event_callback field."""

    def test_event_callback_none_default(self):
        ctx = BackendContext(node={})
        assert ctx.event_callback is None

    def test_event_callback_field(self):
        calls = []

        def cb(event_type: str, payload: dict) -> None:
            calls.append((event_type, payload))

        ctx = BackendContext(node={}, event_callback=cb)
        assert ctx.event_callback is not None
        ctx.event_callback("assistant", {"data": 1})
        assert len(calls) == 1
        assert calls[0] == ("assistant", {"data": 1})


# ---------------------------------------------------------------------------
# BackendResult messages tests
# ---------------------------------------------------------------------------


class TestBackendResultMessages(unittest.TestCase):
    """Tests for BackendResult.messages field."""

    def test_messages_default_empty(self):
        result = BackendResult()
        assert result.messages == []

    def test_messages_field(self):
        msgs = [{"raw_type": "assistant", "data": {}}]
        result = BackendResult(messages=msgs)
        assert len(result.messages) == 1
        assert result.messages[0]["raw_type"] == "assistant"

    def test_messages_in_to_dict_not_promoted(self):
        """messages are not promoted to top-level in to_dict (by design)."""
        result = BackendResult(
            messages=[{"raw_type": "result", "data": {"result": "ok"}}],
        )
        d = result.to_dict()
        assert "messages" not in d


# ---------------------------------------------------------------------------
# ClaudeCodeBackend stream execution tests
# ---------------------------------------------------------------------------


class TestClaudeCodeStreamExecution(unittest.TestCase):
    """Tests for ClaudeCodeBackend CLI stream-json execution."""

    def _make_backend(self):
        from agent.backends.claude_code import (
            ClaudeCodeBackend,
            ClaudeCodeRuntimeConfig,
        )
        config = ClaudeCodeRuntimeConfig(cli_path="claude")
        backend = ClaudeCodeBackend(config)
        backend._cli_available = True
        return backend

    def _make_context(self, **kwargs):
        defaults = {
            "node": MagicMock(
                id="n1",
                agent_type="generator",
                task_description="test",
            ),
            "workspace_path": ".",
        }
        defaults.update(kwargs)
        return BackendContext(**defaults)

    def test_cli_command_uses_stream_json(self):
        backend = self._make_backend()
        ctx = self._make_context()
        cmd = backend._build_cli_command(ctx, "hello")
        assert "--output-format" in cmd
        idx = cmd.index("--output-format")
        assert cmd[idx + 1] == "stream-json"

    def test_stream_output_events(self):
        """Test _stream_cli_output parses NDJSON lines and calls callbacks."""
        backend = self._make_backend()
        parser = StreamParser()
        usage = {"input_tokens": 0, "output_tokens": 0}
        state = {"session_id": "", "result": "", "error": ""}
        events = []

        def event_cb(t, p):
            events.append((t, p))

        async def _run():
            lines = [
                _jl({"type": "system", "session_id": "sess1"}),
                _jl({
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 50,
                        },
                    },
                }),
                _jl({
                    "type": "result",
                    "result": "done",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                    },
                    "session_id": "sess1",
                    "is_error": False,
                }),
                b"",  # EOF
            ]

            process = MagicMock()
            line_iter = iter(lines)

            async def readline():
                return next(line_iter, b"")

            process.stdout.readline = readline
            process.terminate = MagicMock()
            process.kill = MagicMock()

            await backend._stream_cli_output(
                process, parser, usage, state,
                None, None, event_cb,
            )

        asyncio.run(_run())

        assert len(events) == 3
        assert events[0][0] == "system"
        assert events[1][0] == "assistant"
        assert events[2][0] == "result"
        assert state["session_id"] == "sess1"
        assert state["result"] == "done"
        assert usage["input_tokens"] == 100
        assert usage["output_tokens"] == 50

    def test_usage_accumulation(self):
        """Multiple assistant events accumulate usage."""
        backend = self._make_backend()
        parser = StreamParser()
        usage = {"input_tokens": 0, "output_tokens": 0}
        state = {"session_id": "", "result": "", "error": ""}

        async def _run():
            lines = [
                _jl({"type": "assistant", "message": {
                    "usage": {"input_tokens": 50, "output_tokens": 25},
                }}),
                _jl({"type": "assistant", "message": {
                    "usage": {"input_tokens": 30, "output_tokens": 15},
                }}),
                b"",
            ]
            process = MagicMock()
            line_iter = iter(lines)

            async def readline():
                return next(line_iter, b"")

            process.stdout.readline = readline
            process.terminate = MagicMock()
            process.kill = MagicMock()

            await backend._stream_cli_output(
                process, parser, usage, state,
                None, None, None,
            )

        asyncio.run(_run())
        assert usage["input_tokens"] == 80
        assert usage["output_tokens"] == 40

    def test_usage_result_override(self):
        """Result event usage overrides accumulated values."""
        backend = self._make_backend()
        parser = StreamParser()
        usage = {"input_tokens": 0, "output_tokens": 0}
        state = {"session_id": "", "result": "", "error": ""}

        async def _run():
            lines = [
                _jl({"type": "assistant", "message": {
                    "usage": {"input_tokens": 50, "output_tokens": 25},
                }}),
                _jl({"type": "result", "result": "ok", "usage": {
                    "input_tokens": 200, "output_tokens": 100,
                }}),
                b"",
            ]
            process = MagicMock()
            line_iter = iter(lines)

            async def readline():
                return next(line_iter, b"")

            process.stdout.readline = readline
            process.terminate = MagicMock()
            process.kill = MagicMock()

            await backend._stream_cli_output(
                process, parser, usage, state,
                None, None, None,
            )

        asyncio.run(_run())
        assert usage["input_tokens"] == 200
        assert usage["output_tokens"] == 100

    def test_cancel_mid_stream(self):
        """cancel_event terminates process early."""
        backend = self._make_backend()
        parser = StreamParser()
        usage = {"input_tokens": 0, "output_tokens": 0}
        state = {"session_id": "", "result": "", "error": ""}
        cancel_event = threading.Event()
        cancel_event.set()

        async def _run():
            process = MagicMock()

            async def readline():
                return b'{"type":"assistant","message":{}}\n'

            process.stdout.readline = readline
            process.terminate = MagicMock()
            process.kill = MagicMock()
            process.wait = AsyncMock()

            await backend._stream_cli_output(
                process, parser, usage, state,
                cancel_event, None, None,
            )
            process.terminate.assert_called_once()

        asyncio.run(_run())

    def test_progress_callback_called(self):
        """progress_callback is called for each valid message."""
        backend = self._make_backend()
        parser = StreamParser()
        usage = {"input_tokens": 0, "output_tokens": 0}
        state = {"session_id": "", "result": "", "error": ""}
        progress_calls = [0]

        def on_progress():
            progress_calls[0] += 1

        async def _run():
            lines = [
                b'{"type":"assistant","message":{}}\n',
                b'{"type":"result","result":"ok"}\n',
                b"",
            ]
            process = MagicMock()
            line_iter = iter(lines)

            async def readline():
                return next(line_iter, b"")

            process.stdout.readline = readline
            process.terminate = MagicMock()
            process.kill = MagicMock()

            await backend._stream_cli_output(
                process, parser, usage, state,
                None, on_progress, None,
            )

        asyncio.run(_run())
        assert progress_calls[0] == 2

    def test_build_stream_result_completed(self):
        backend = self._make_backend()
        parser = StreamParser()
        parser.feed_line('{"type":"assistant","message":{}}')
        parser.feed_line('{"type":"result","result":"hello world"}')
        usage = {"input_tokens": 100, "output_tokens": 50}
        state = {"session_id": "s1", "result": "hello world", "error": ""}
        result = backend._build_stream_result(parser, usage, state, ["file.py"])
        assert result.status == BackendStatus.COMPLETED
        assert result.output == "hello world"
        assert result.metadata["session_id"] == "s1"
        assert len(result.messages) == 2

    def test_build_stream_result_error(self):
        backend = self._make_backend()
        parser = StreamParser()
        parser.feed_line('{"type":"result","result":"boom","is_error":true}')
        usage = {"input_tokens": 0, "output_tokens": 0}
        state = {"session_id": "", "result": "boom", "error": "boom"}
        result = backend._build_stream_result(parser, usage, state, [])
        assert result.status == BackendStatus.FAILED
        assert result.error == "boom"


# ---------------------------------------------------------------------------
# SDK result messages tests
# ---------------------------------------------------------------------------


class TestSDKResultMessages(unittest.TestCase):
    """Tests for SDK path producing messages."""

    def _make_backend(self):
        from agent.backends.claude_code import (
            ClaudeCodeBackend,
            ClaudeCodeRuntimeConfig,
        )
        config = ClaudeCodeRuntimeConfig(cli_path="claude")
        return ClaudeCodeBackend(config)

    def _make_context(self):
        return BackendContext(
            node=MagicMock(
                id="n1",
                agent_type="generator",
                task_description="test",
            ),
            workspace_path=".",
        )

    def test_sdk_result_has_messages(self):
        backend = self._make_backend()
        ctx = self._make_context()
        raw = {"result": "hello", "is_error": False, "usage": {}}
        result = backend._parse_sdk_result(raw, ctx)
        assert result.status == BackendStatus.COMPLETED
        assert len(result.messages) == 1
        assert result.messages[0]["raw_type"] == "result"

    def test_sdk_error_has_messages(self):
        backend = self._make_backend()
        ctx = self._make_context()
        raw = {"result": "oops", "is_error": True, "errors": ["bad"], "usage": {}}
        result = backend._parse_sdk_result(raw, ctx)
        assert result.status == BackendStatus.FAILED
        assert len(result.messages) == 1


if __name__ == "__main__":
    unittest.main()
