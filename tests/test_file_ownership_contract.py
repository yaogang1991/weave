"""Tests for file ownership contract model and plan validator conflict detection (#272)."""
import pytest

from core.models import (  # noqa: F401
    FileAccessPolicy,
    FileOwnershipContract,
    ConflictResolution,
    DAGNode,
    NodeStatus,
)
from orchestrator.plan_validator import PlanValidator, PlanValidationError


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

class TestFileOwnershipContractModel:
    def test_contract_defaults(self):
        c = FileOwnershipContract(node_id="n1")
        assert c.node_id == "n1"
        assert c.owned_files == []
        assert c.forbidden_files == []
        assert c.shared_files == []
        assert c.access_policy == {}

    def test_contract_with_files(self):
        c = FileOwnershipContract(
            node_id="n1",
            owned_files=["src/lib.py", "src/__init__.py"],
            forbidden_files=["src/other.py"],
            shared_files=["src/config.py"],
        )
        assert len(c.owned_files) == 2
        assert "src/other.py" in c.forbidden_files

    def test_contract_serialization(self):
        c = FileOwnershipContract(
            node_id="n1",
            owned_files=["a.py"],
            access_policy={"a.py": FileAccessPolicy.OWNED},
        )
        data = c.model_dump()
        c2 = FileOwnershipContract(**data)
        assert c2.node_id == c.node_id
        assert c2.owned_files == c.owned_files
        assert c2.access_policy["a.py"] == FileAccessPolicy.OWNED

    def test_access_policy_enum(self):
        assert FileAccessPolicy.OWNED == "owned"
        assert FileAccessPolicy.FORBIDDEN == "forbidden"
        assert FileAccessPolicy.SHARED == "shared"

    def test_conflict_resolution_enum(self):
        assert ConflictResolution.SERIALIZE == "serialize"
        assert ConflictResolution.ERROR == "error"


class TestDAGNodeOwnedFiles:
    def test_node_defaults_empty_owned_files(self):
        node = DAGNode(id="n1", agent_type="generator", task_description="test")
        assert node.owned_files == []

    def test_node_with_owned_files(self):
        node = DAGNode(
            id="n1",
            agent_type="generator",
            task_description="test",
            owned_files=["src/a.py", "src/b.py"],
        )
        assert node.owned_files == ["src/a.py", "src/b.py"]

    def test_node_serialization_with_owned_files(self):
        node = DAGNode(
            id="n1",
            agent_type="generator",
            task_description="test",
            owned_files=["src/a.py"],
        )
        data = node.model_dump()
        assert data["owned_files"] == ["src/a.py"]
        node2 = DAGNode(**data)
        assert node2.owned_files == ["src/a.py"]


# ---------------------------------------------------------------------------
# Plan validator conflict detection tests
# ---------------------------------------------------------------------------

def _make_plan(nodes, edges=None):
    """Helper to build a plan dict."""
    return {
        "reasoning": "test",
        "nodes": nodes,
        "edges": edges or [],
    }


class TestParallelWriteConflictDetection:
    def test_no_conflict_passes(self):
        """Two parallel nodes with disjoint owned_files pass validation."""
        plan = _make_plan(
            nodes=[
                {"id": "g1", "agent_type": "generator", "task": "impl A",
                 "owned_files": ["src/a.py"]},
                {"id": "g2", "agent_type": "generator", "task": "impl B",
                 "owned_files": ["src/b.py"]},
                {"id": "e1", "agent_type": "evaluator", "task": "eval"},
            ],
            edges=[],
        )
        v = PlanValidator()
        v.validate(plan)
        # No PlanValidationError raised
        assert not any("ownership" in w.lower() for w in v.warnings)

    def test_init_py_collision_detected(self):
        """Two parallel generators both owning __init__.py → error."""
        plan = _make_plan(
            nodes=[
                {"id": "g1", "agent_type": "generator", "task": "impl A",
                 "owned_files": ["pkg/__init__.py", "pkg/a.py"]},
                {"id": "g2", "agent_type": "generator", "task": "impl B",
                 "owned_files": ["pkg/__init__.py", "pkg/b.py"]},
            ],
            edges=[],
        )
        v = PlanValidator()
        with pytest.raises(PlanValidationError, match="__init__.py"):
            v.validate(plan)

    def test_same_file_overlap_detected(self):
        """Two nodes both owning the same file → error."""
        plan = _make_plan(
            nodes=[
                {"id": "g1", "agent_type": "generator", "task": "impl A",
                 "owned_files": ["src/lib/parser.py"]},
                {"id": "g2", "agent_type": "generator", "task": "impl B",
                 "owned_files": ["src/lib/parser.py"]},
            ],
            edges=[],
        )
        v = PlanValidator()
        with pytest.raises(PlanValidationError, match="same files"):
            v.validate(plan)

    def test_shared_file_with_merge_node_passes(self):
        """Two nodes share a file but have a downstream merge node → warning."""
        plan = _make_plan(
            nodes=[
                {"id": "g1", "agent_type": "generator", "task": "impl A",
                 "owned_files": ["src/config.py", "src/a.py"]},
                {"id": "g2", "agent_type": "generator", "task": "impl B",
                 "owned_files": ["src/config.py", "src/b.py"]},
                {"id": "merge", "agent_type": "evaluator", "task": "merge eval"},
            ],
            edges=[
                {"from": "g1", "to": "merge"},
                {"from": "g2", "to": "merge"},
            ],
        )
        v = PlanValidator()
        v.validate(plan)
        # Should produce a warning about shared files, not an error
        assert any("share files" in w for w in v.warnings)

    def test_no_owned_files_emits_serialization_warning(self):
        """Parallel generators without owned_files → serialization warning."""
        plan = _make_plan(
            nodes=[
                {"id": "g1", "agent_type": "generator", "task": "impl A"},
                {"id": "g2", "agent_type": "generator", "task": "impl B"},
            ],
            edges=[],
        )
        v = PlanValidator()
        v.validate(plan)
        assert any("serialization" in w for w in v.warnings)

    def test_single_generator_no_parallel_check(self):
        """Single generator doesn't trigger parallel checks."""
        plan = _make_plan(
            nodes=[
                {"id": "g1", "agent_type": "generator", "task": "impl A",
                 "owned_files": ["src/a.py"]},
            ],
            edges=[],
        )
        v = PlanValidator()
        v.validate(plan)
        assert not any("parallel" in w.lower() for w in v.warnings)

    def test_sequential_generators_no_conflict(self):
        """Generators in sequence (one depends on the other) don't conflict."""
        plan = _make_plan(
            nodes=[
                {"id": "g1", "agent_type": "generator", "task": "impl A",
                 "owned_files": ["src/a.py"]},
                {"id": "g2", "agent_type": "generator", "task": "impl B",
                 "owned_files": ["src/a.py"]},
            ],
            edges=[{"from": "g1", "to": "g2"}],
        )
        v = PlanValidator()
        v.validate(plan)
        # Sequential nodes, so no parallel conflict
        assert not any("same files" in w for w in v.warnings)

    def test_mixed_parallel_and_sequential(self):
        """Two parallel generators + one sequential after both."""
        plan = _make_plan(
            nodes=[
                {"id": "g1", "agent_type": "generator", "task": "impl A",
                 "owned_files": ["src/a.py"]},
                {"id": "g2", "agent_type": "generator", "task": "impl B",
                 "owned_files": ["src/b.py"]},
                {"id": "g3", "agent_type": "generator", "task": "impl C",
                 "owned_files": ["src/c.py"]},
            ],
            edges=[
                {"from": "g1", "to": "g3"},
                {"from": "g2", "to": "g3"},
            ],
        )
        v = PlanValidator()
        v.validate(plan)
        # g1 and g2 are parallel, but disjoint owned_files → no error
        assert not any("same files" in w for w in v.warnings)
