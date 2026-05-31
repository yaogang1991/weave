"""Tests for the exception hierarchy and classify_error (#1007 Phase 4)."""
import pytest

from core.exceptions import (
    AgentExecutionError,
    BackendError,
    BudgetExhaustedError,
    ConfigurationError,
    GuardrailBlockedException,
    HardTimeoutError,
    HookError,
    MCPError,
    MemoryStoreError,
    NodeTimeoutError,
    PendingApprovalError,
    PlanValidationError,
    RateLimitError,
    WasmRuntimeError,
    WeaveError,
    ExecutionError,
    PlanningError,
    InfrastructureError,
    WorkflowError,
)
from control_plane.errors import classify_error


# ---------------------------------------------------------------------------
# Hierarchy tests
# ---------------------------------------------------------------------------


class TestExceptionHierarchy:
    """Verify isinstance relationships for all exception types."""

    def test_base_hierarchy(self):
        assert issubclass(ExecutionError, WeaveError)
        assert issubclass(PlanningError, WeaveError)
        assert issubclass(InfrastructureError, WeaveError)
        assert issubclass(WorkflowError, WeaveError)

    def test_execution_errors(self):
        assert issubclass(NodeTimeoutError, ExecutionError)
        assert issubclass(HardTimeoutError, NodeTimeoutError)
        assert issubclass(BudgetExhaustedError, ExecutionError)
        assert issubclass(GuardrailBlockedException, ExecutionError)
        assert issubclass(AgentExecutionError, ExecutionError)

    def test_planning_errors(self):
        assert issubclass(PlanValidationError, PlanningError)

    def test_infrastructure_errors(self):
        assert issubclass(RateLimitError, InfrastructureError)
        assert issubclass(HookError, InfrastructureError)
        assert issubclass(WasmRuntimeError, InfrastructureError)
        assert issubclass(ConfigurationError, InfrastructureError)
        assert issubclass(BackendError, InfrastructureError)
        assert issubclass(MCPError, InfrastructureError)
        assert issubclass(MemoryStoreError, InfrastructureError)

    def test_workflow_errors(self):
        assert issubclass(PendingApprovalError, WorkflowError)

    def test_new_types_catchable_by_base(self):
        infra_types = [
            ConfigurationError("test"),
            BackendError("test"),
            MCPError("server", "op"),
            MemoryStoreError("test"),
        ]
        for exc in infra_types:
            assert isinstance(exc, WeaveError)
            assert isinstance(exc, InfrastructureError)

        # AgentExecutionError is an ExecutionError, not InfrastructureError
        assert isinstance(AgentExecutionError("test"), ExecutionError)

    def test_mcp_error_attributes(self):
        exc = MCPError("my_server", "connect")
        assert exc.server_name == "my_server"
        assert exc.operation == "connect"
        assert "my_server" in str(exc)
        assert "connect" in str(exc)


# ---------------------------------------------------------------------------
# classify_error tests
# ---------------------------------------------------------------------------


class TestClassifyError:
    """Verify classify_error maps new exception types correctly."""

    def test_configuration_error(self):
        assert classify_error(ConfigurationError("bad config")) == "configuration_error"

    def test_backend_error(self):
        assert classify_error(BackendError("workspace unavailable")) == "backend_error"

    def test_agent_execution_error(self):
        assert classify_error(AgentExecutionError("pool missing")) == "agent_error"

    def test_mcp_error(self):
        assert classify_error(MCPError("srv", "connect")) == "mcp_error"

    def test_memory_store_error(self):
        assert classify_error(MemoryStoreError("persist failed")) == "memory_error"

    def test_existing_rate_limit(self):
        exc = RateLimitError("anthropic", "claude-3", 3)
        assert classify_error(exc) == "rate_limit"

    def test_existing_timeout(self):
        exc = NodeTimeoutError("node1", "generator", 60)
        assert classify_error(exc) == "timeout"

    def test_string_fallback(self):
        assert classify_error("something went wrong") == "unknown"
