"""Execution backends: local, worktree + sandbox providers: local, docker(stub)."""

from __future__ import annotations

from backend.base import ExecutionBackend, WorkspaceIsolation, ExecutionSandbox
from backend.local import LocalBackend
from backend.worktree import WorktreeBackend
from backend.sandbox import SandboxProvider, LocalSandbox, DockerSandbox
from backend.lifecycle import BackendManager, HookResult, HookError

__all__ = [
    "ExecutionBackend",
    "WorkspaceIsolation",
    "ExecutionSandbox",
    "LocalBackend",
    "WorktreeBackend",
    "SandboxProvider",
    "LocalSandbox",
    "DockerSandbox",
    "BackendManager",
    "HookResult",
    "HookError",
]
