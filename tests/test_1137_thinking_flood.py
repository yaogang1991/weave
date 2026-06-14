"""Tests for #1137 (track 2): thinking-token flood fast-fail.

StreamParser now tracks consecutive thinking-token stream events, and
ClaudeCodeBackend._stream_cli_output raises ThinkingFloodError once the
streak crosses THINKING_FLOOD_THRESHOLD — turning a multi-minute wall-clock
hang into a fast-fail. ThinkingFloodError is treated as a model-behavior
error that does not consume retry budget (like NodeTimeoutError).
"""
import json

import pytest

from agent.backends.stream_parser import StreamParser
from core.exceptions import ThinkingFloodError, ExecutionError


def _thinking_line() -> str:
    return json.dumps(
        {"type": "system", "subtype": "thinking_tokens", "delta": 5}
    )


def _assistant_line() -> str:
    return json.dumps({
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": "hi"}],
            "session_id": "s1",
        },
    })


class TestStreamParserThinkingStreak:
    """StreamParser tracks consecutive thinking-token events (#1137)."""

    def test_starts_at_zero(self):
        assert StreamParser().thinking_streak == 0

    def test_thinking_tokens_increments_streak(self):
        parser = StreamParser()
        parser.feed_line(_thinking_line())
        assert parser.thinking_streak == 1
        parser.feed_line(_thinking_line())
        assert parser.thinking_streak == 2

    def test_productive_event_resets_streak(self):
        parser = StreamParser()
        parser.feed_line(_thinking_line())
        parser.feed_line(_thinking_line())
        assert parser.thinking_streak == 2
        parser.feed_line(_assistant_line())
        assert parser.thinking_streak == 0

    def test_tool_result_resets_streak(self):
        parser = StreamParser()
        parser.feed_line(_thinking_line())
        parser.feed_line(json.dumps({"type": "tool_result", "content": "ok"}))
        assert parser.thinking_streak == 0

    def test_non_thinking_system_event_does_not_increment(self):
        parser = StreamParser()
        parser.feed_line(json.dumps({"type": "system", "subtype": "init"}))
        assert parser.thinking_streak == 0


class TestThinkingFloodError:
    def test_is_execution_error(self):
        err = ThinkingFloodError("n1", "generator", 50, 50)
        assert isinstance(err, ExecutionError)

    def test_attributes_and_message(self):
        err = ThinkingFloodError("n1", "generator", 73, 50)
        assert err.node_id == "n1"
        assert err.agent_type == "generator"
        assert err.streak == 73
        assert err.threshold == 50
        msg = str(err)
        assert "73" in msg
        assert "thinking" in msg.lower()


class _FakeStdout:
    def __init__(self, lines):
        self._lines = [ln + "\n" for ln in lines]

    async def readline(self):
        if self._lines:
            return self._lines.pop(0).encode()
        return b""  # EOF


class _FakeProc:
    def __init__(self, lines):
        self.stdout = _FakeStdout(lines)
        self.stderr = None


def _make_backend():
    from agent.backends.claude_code import (
        ClaudeCodeBackend, ClaudeCodeRuntimeConfig,
    )
    return ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())


class TestStreamCliOutputFloodDetection:
    """_stream_cli_output raises ThinkingFloodError on a thinking flood."""

    @pytest.mark.asyncio
    async def test_flood_raises_thinking_flood_error(self, monkeypatch):
        from agent.backends import claude_code as cc
        monkeypatch.setattr(cc, "THINKING_FLOOD_THRESHOLD", 3)

        backend = _make_backend()
        parser = StreamParser()

        with pytest.raises(ThinkingFloodError) as ei:
            await backend._stream_cli_output(
                _FakeProc([_thinking_line()] * 3), parser,
                usage={"input_tokens": 0, "output_tokens": 0},
                state={"session_id": "", "result": "", "error": ""},
                cancel_event=None, progress_callback=None,
                event_callback=None,
                node_id="n1", agent_type="generator",
            )
        assert ei.value.streak >= 3
        assert ei.value.node_id == "n1"
        assert ei.value.agent_type == "generator"

    @pytest.mark.asyncio
    async def test_normal_stream_does_not_raise(self, monkeypatch):
        from agent.backends import claude_code as cc
        monkeypatch.setattr(cc, "THINKING_FLOOD_THRESHOLD", 50)

        backend = _make_backend()
        parser = StreamParser()

        # Thinking events interspersed with productive assistant events —
        # streak never approaches the threshold and resets each time.
        lines = [
            _thinking_line(), _thinking_line(), _assistant_line(),
            _thinking_line(), _thinking_line(), _assistant_line(),
        ]

        await backend._stream_cli_output(
            _FakeProc(lines), parser,
            usage={"input_tokens": 0, "output_tokens": 0},
            state={"session_id": "", "result": "", "error": ""},
            cancel_event=None, progress_callback=None,
            event_callback=None,
            node_id="n1", agent_type="generator",
        )
        assert parser.thinking_streak == 0
