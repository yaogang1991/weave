"""
Tests for #180 PR4: core/models.py domain module split.

Verifies:
1. All classes importable from core.models (backward compat)
2. Domain modules independently importable
3. Cross-domain identity (same class object via both paths)
4. DAGNode validator works with SuccessCriterion from eval_models
"""

from core.eval_models import SuccessCriterion


class TestBackwardCompatImports:
    """All classes must remain importable from core.models."""

    def test_dag_domain(self):
        from core.models import (  # noqa: F401
            AgentCapability, NodeStatus, NodeHealth,
            NodeWorkspaceStrategy, NodeWorkspace, NodeWorkspaceResult,
            FileAccessPolicy, ConflictResolution, FileOwnershipContract,
            DAGNode, DependencyType, DAGEdge, DAG,
            ExecutionEvent, FailureDecision, OrchestratorPlan, HandoffArtifact,
        )
        assert DAG is not None

    def test_tool_domain(self):
        from core.models import ToolCall, ToolResult, AgentMessage  # noqa: F401
        assert ToolCall is not None

    def test_eval_domain(self):
        from core.models import EvalStatus, EvaluationResult, CriterionType, SuccessCriterion  # noqa: F401
        assert SuccessCriterion is not None

    def test_event_domain(self):
        from core.models import EventType, Event, SessionMetrics, SessionState  # noqa: F401
        assert SessionState is not None

    def test_guardrail_domain(self):
        from core.models import RiskLevel, PermissionMode, GuardrailPolicy, PersonalGuardrailPolicy  # noqa: F401
        assert PersonalGuardrailPolicy is not None

    def test_memory_domain(self):
        from core.models import (  # noqa: F401
            MemoryScope, MemoryType, MemoryEntry,
            LearningCategory, InsightType, LearningInsight,
        )
        assert MemoryEntry is not None

    def test_analysis_domain(self):
        from core.models import DAGTemplate, ImpactScope, VerificationResult  # noqa: F401
        assert VerificationResult is not None

    def test_mcp_domain(self):
        from core.models import MCPServerStatus, Skill  # noqa: F401
        assert Skill is not None


class TestDirectDomainImports:
    """Domain modules must be independently importable."""

    def test_eval_models(self):
        from core.eval_models import EvalStatus, EvaluationResult, CriterionType, SuccessCriterion  # noqa: F401
        assert EvalStatus.CLEAN_PASS is not None

    def test_tool_models(self):
        from core.tool_models import ToolCall, ToolResult, AgentMessage  # noqa: F401
        assert ToolCall is not None

    def test_dag_models(self):
        from core.dag_models import DAG, DAGNode, DAGEdge, NodeStatus, NodeHealth  # noqa: F401
        assert DAG is not None

    def test_event_models(self):
        from core.event_models import EventType, Event, SessionState, SessionMetrics  # noqa: F401
        assert EventType.SESSION_START is not None

    def test_guardrail_models(self):
        from core.guardrail_models import RiskLevel, PermissionMode, GuardrailPolicy  # noqa: F401
        assert RiskLevel.HIGH == 3

    def test_memory_models(self):
        from core.memory_models import MemoryEntry, MemoryScope, LearningInsight  # noqa: F401
        assert MemoryScope.GLOBAL is not None

    def test_analysis_models(self):
        from core.analysis_models import DAGTemplate, ImpactScope  # noqa: F401
        assert DAGTemplate is not None

    def test_mcp_models(self):
        from core.mcp_models import MCPServerStatus, Skill  # noqa: F401
        assert MCPServerStatus.CONNECTED is not None


class TestCrossDomainIdentity:
    """Classes imported via different paths must be the same object."""

    def test_dag_node_identity(self):
        from core.models import DAGNode
        from core.dag_models import DAGNode as DirectDAGNode
        assert DAGNode is DirectDAGNode

    def test_success_criterion_identity(self):
        from core.models import SuccessCriterion
        from core.eval_models import SuccessCriterion as DirectSC
        assert SuccessCriterion is DirectSC

    def test_event_type_identity(self):
        from core.models import EventType
        from core.event_models import EventType as DirectET
        assert EventType is DirectET

    def test_memory_entry_identity(self):
        from core.models import MemoryEntry
        from core.memory_models import MemoryEntry as DirectME
        assert MemoryEntry is DirectME

    def test_skill_identity(self):
        from core.models import Skill
        from core.mcp_models import Skill as DirectSkill
        assert Skill is DirectSkill


class TestCrossDomainValidator:
    """DAGNode validator must work with SuccessCriterion from eval_models."""

    def test_string_criteria(self):
        from core.models import DAGNode
        node = DAGNode(
            id="test", agent_type="gen", task_description="t",
            success_criteria=["tests pass"],
        )
        assert isinstance(node.success_criteria[0], str)

    def test_dict_criteria_parsed(self):
        from core.models import DAGNode
        node = DAGNode(
            id="test", agent_type="gen", task_description="t",
            success_criteria=[{"type": "tests_pass", "test_path": "tests/"}],
        )
        assert isinstance(node.success_criteria[0], SuccessCriterion)
        assert node.success_criteria[0].type.value == "tests_pass"

    def test_json_string_criteria(self):
        from core.models import DAGNode
        node = DAGNode(
            id="test", agent_type="gen", task_description="t",
            success_criteria=['{"type": "lint"}'],
        )
        assert isinstance(node.success_criteria[0], SuccessCriterion)

    def test_success_criterion_object(self):
        from core.models import DAGNode
        sc = SuccessCriterion(type="tests_pass", test_path="tests/")
        node = DAGNode(
            id="test", agent_type="gen", task_description="t",
            success_criteria=[sc],
        )
        assert isinstance(node.success_criteria[0], SuccessCriterion)
