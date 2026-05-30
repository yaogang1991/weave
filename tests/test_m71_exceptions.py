"""Tests for M7.1 Phase 4: Exception types + classify_error + ValueError migration."""

from __future__ import annotations

import pytest

from core.exceptions import (
    AgentExecutionError,
    BackendError,
    ConfigurationError,
    InfrastructureError,
    MCPError,
    MemoryStoreError,
    WeaveError,
)
from control_plane.errors import classify_error


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------

def test_configuration_error_is_infrastructure():
    assert issubclass(ConfigurationError, InfrastructureError)
    assert issubclass(ConfigurationError, WeaveError)


def test_backend_error_is_infrastructure():
    assert issubclass(BackendError, InfrastructureError)
    assert issubclass(BackendError, WeaveError)


def test_agent_execution_error_is_infrastructure():
    assert issubclass(AgentExecutionError, InfrastructureError)
    assert issubclass(AgentExecutionError, WeaveError)


def test_mcp_error_is_infrastructure():
    assert issubclass(MCPError, InfrastructureError)
    assert issubclass(MCPError, WeaveError)


def test_memory_store_error_is_infrastructure():
    assert issubclass(MemoryStoreError, InfrastructureError)
    assert issubclass(MemoryStoreError, WeaveError)


# ---------------------------------------------------------------------------
# classify_error — new branches
# ---------------------------------------------------------------------------

def test_classify_hook_error():
    from core.exceptions import HookError
    assert classify_error(HookError("test")) == "hook_error"


def test_classify_wasm_error():
    from core.exceptions import WasmRuntimeError
    assert classify_error(WasmRuntimeError("wasm fail")) == "wasm_error"


def test_classify_configuration_error():
    assert classify_error(ConfigurationError("bad config")) == "configuration_error"


def test_classify_backend_error():
    assert classify_error(BackendError("backend fail")) == "backend_error"


def test_classify_agent_error():
    assert classify_error(AgentExecutionError("sdk fail")) == "agent_error"


def test_classify_mcp_error():
    assert classify_error(MCPError("mcp fail")) == "mcp_error"


def test_classify_memory_error():
    assert classify_error(MemoryStoreError("store fail")) == "memory_error"


def test_classify_infrastructure_fallback():
    assert classify_error(InfrastructureError("generic infra")) == "infrastructure_error"


# ---------------------------------------------------------------------------
# ValueError migration — backend/lifecycle.py
# ---------------------------------------------------------------------------

def test_backend_lifecycle_raises_backend_error():
    from backend.lifecycle import BackendManager

    with pytest.raises(BackendError, match="Invalid cleanup_policy"):
        BackendManager(cleanup_policy="invalid_policy")
