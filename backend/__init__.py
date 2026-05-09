"""Execution backends: local, worktree, docker(stub)."""

from __future__ import annotations

from backend.base import ExecutionBackend, BackendType
from backend.local import LocalBackend
from backend.worktree import WorktreeBackend
from backend.lifecycle import BackendManager

__all__ = [
    "ExecutionBackend",
    "BackendType",
    "LocalBackend",
    "WorktreeBackend",
    "BackendManager",
]
