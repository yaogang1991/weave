"""Agent backends -- pluggable execution strategies for DAG nodes."""
from agent.backends.base import AgentBackend
from agent.backends.registry import BackendRegistry
from agent.backends.builtin import BuiltinBackend
from agent.backends.codex import CodexBackend

__all__ = ["AgentBackend", "BackendRegistry", "BuiltinBackend", "CodexBackend"]


def __getattr__(name: str):
    if name == "ClaudeCodeBackend":
        from agent.backends.claude_code import ClaudeCodeBackend
        return ClaudeCodeBackend
    if name == "ClaudeCodeConfig":
        from agent.backends.claude_code import ClaudeCodeConfig
        return ClaudeCodeConfig
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
