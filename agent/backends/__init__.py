"""Agent backends -- pluggable execution strategies for DAG nodes."""
from agent.backends.base import AgentBackend
from agent.backends.registry import BackendRegistry
from agent.backends.builtin import BuiltinBackend

__all__ = ["AgentBackend", "BackendRegistry", "BuiltinBackend"]
