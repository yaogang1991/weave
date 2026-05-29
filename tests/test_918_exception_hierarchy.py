"""Tests for #918: unified exception hierarchy in core/exceptions.py.

Verifies:
1. Hierarchy structure (isinstance checks)
2. Backward-compatible imports from original locations
3. HardTimeoutError dual inheritance (TimeoutError + NodeTimeoutError)
4. Catch-by-base-class works (except ExecutionError, etc.)
"""
import pytest

from core.exceptions import (
    WeaveError,
    ExecutionError,
    PlanningError,
    InfrastructureError,
    WorkflowError,
    NodeTimeoutError,
    HardTimeoutError,
    BudgetExhaustedError,
    GuardrailBlockedException,
    PlanValidationError,
    RateLimitError,
    HookError,
    WasmRuntimeError,
    PendingApprovalError,
)


class TestHierarchyStructure:
    """Verify every exception sits in the correct branch."""

    def test_all_leaf_exceptions_are_weave_errors(self):
        """Every custom exception should be a WeaveError."""
        leafs = [
            NodeTimeoutError("n1", "gen", 30),
            HardTimeoutError("n1", "gen", 30),
            BudgetExhaustedError(100, 200),
            GuardrailBlockedException("unsafe"),
            PlanValidationError("bad plan"),
            RateLimitError("anthropic", "sonnet", 3),
            HookError("after_create", error="fail"),
            WasmRuntimeError("wasm crash"),
            PendingApprovalError("ticket_1"),
        ]
        for exc in leafs:
            assert isinstance(exc, WeaveError), f"{type(exc).__name__} not WeaveError"

    def test_execution_error_branch(self):
        assert issubclass(NodeTimeoutError, ExecutionError)
        assert issubclass(HardTimeoutError, ExecutionError)
        assert issubclass(BudgetExhaustedError, ExecutionError)
        assert issubclass(GuardrailBlockedException, ExecutionError)

    def test_planning_error_branch(self):
        assert issubclass(PlanValidationError, PlanningError)

    def test_infrastructure_error_branch(self):
        assert issubclass(RateLimitError, InfrastructureError)
        assert issubclass(HookError, InfrastructureError)
        assert issubclass(WasmRuntimeError, InfrastructureError)

    def test_workflow_error_branch(self):
        assert issubclass(PendingApprovalError, WorkflowError)

    def test_base_classes_are_weave_errors(self):
        for cls in (ExecutionError, PlanningError, InfrastructureError, WorkflowError):
            assert issubclass(cls, WeaveError)

    def test_branches_are_disjoint(self):
        """No branch should inherit from another branch."""
        branches = [ExecutionError, PlanningError, InfrastructureError, WorkflowError]
        for i, a in enumerate(branches):
            for j, b in enumerate(branches):
                if i != j:
                    assert not issubclass(a, b), f"{a.__name__} inherits from {b.__name__}"


class TestHardTimeoutErrorCompat:
    """Verify HardTimeoutError backward compatibility."""

    def test_is_node_timeout_error(self):
        exc = HardTimeoutError("n1", "gen", 30)
        assert isinstance(exc, NodeTimeoutError)

    def test_is_timeout_error(self):
        """Must also be TimeoutError for except TimeoutError compat."""
        exc = HardTimeoutError("n1", "gen", 30)
        assert isinstance(exc, TimeoutError)

    def test_legacy_string_init(self):
        """Old _HardTimeoutError("message") style still works."""
        exc = HardTimeoutError("LLM call exceeded hard timeout of 150s")
        assert isinstance(exc, HardTimeoutError)
        assert isinstance(exc, NodeTimeoutError)
        assert isinstance(exc, TimeoutError)
        assert "hard timeout" in str(exc)

    def test_full_init(self):
        exc = HardTimeoutError("node_1", "generator", 60)
        assert exc.node_id == "node_1"
        assert exc.agent_type == "generator"
        assert exc.timeout == 60


class TestCatchByBaseClass:
    """Verify that catching by intermediate base works."""

    def test_catch_execution_error(self):
        with pytest.raises(ExecutionError):
            raise NodeTimeoutError("n1", "gen", 30)

    def test_catch_planning_error(self):
        with pytest.raises(PlanningError):
            raise PlanValidationError("cycle detected")

    def test_catch_infrastructure_error(self):
        with pytest.raises(InfrastructureError):
            raise RateLimitError("anthropic", "sonnet", 3)

    def test_catch_workflow_error(self):
        with pytest.raises(WorkflowError):
            raise PendingApprovalError("ticket_1")

    def test_catch_weave_error_catches_all(self):
        """except WeaveError catches every custom exception."""
        for exc_cls, args in [
            (NodeTimeoutError, ("n1", "gen", 30)),
            (PlanValidationError, ("bad",)),
            (RateLimitError, ("p", "m", 1)),
            (PendingApprovalError, ("t1",)),
        ]:
            with pytest.raises(WeaveError):
                raise exc_cls(*args)


class TestBackwardCompatImports:
    """Verify imports from original locations still work."""

    def test_plan_validation_error_from_validator(self):
        from orchestrator.plan_validator import PlanValidationError as PVE
        assert PVE is PlanValidationError

    def test_hook_error_from_lifecycle(self):
        from backend.lifecycle import HookError as HE
        assert HE is HookError

    def test_wasm_runtime_error_from_wasm(self):
        from backend.wasm import WasmRuntimeError as WRE
        assert WRE is WasmRuntimeError

    def test_hard_timeout_alias_in_llm_client(self):
        from core.llm_client import _HardTimeoutError as HTE
        assert HTE is HardTimeoutError
