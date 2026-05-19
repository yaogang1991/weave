"""Agent backends -- pluggable execution strategies for DAG nodes."""
from agent.backends.base import AgentBackend
from agent.backends.registry import BackendRegistry
from agent.backends.builtin import BuiltinBackend

__all__ = [
    "AgentBackend",
    "BackendRegistry",
    "BuiltinBackend",
    "ClaudeCodeBackend",
    "ClaudeCodeRuntimeConfig",
    "CodexBackend",
]


def __getattr__(name: str):
    if name == "ClaudeCodeBackend":
        from agent.backends.claude_code import ClaudeCodeBackend
        return ClaudeCodeBackend
    if name == "ClaudeCodeRuntimeConfig":
        from agent.backends.claude_code import ClaudeCodeRuntimeConfig
        return ClaudeCodeRuntimeConfig
    if name == "CodexBackend":
        from agent.backends.codex import CodexBackend
        return CodexBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
