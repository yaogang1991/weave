"""
Tests for #177 PR4: ArtifactHandoffService extraction from DAGExecutionEngine.

Verifies artifact collection, eval result passing, retry feedback,
error-type guidance, and soft dependency warnings.
"""

from core.artifact_handoff import (
    ArtifactHandoffService,
    _has_import_error,
    _has_type_error,
    _has_timeout,
    _has_coverage_low,
    _has_runtime_error,
    _has_init_import_error,
)
from core.models import (  # noqa: F401
    DAG,
    DAGNode,
    DAGEdge,
    HandoffArtifact,
    NodeStatus,
    DependencyType,
)


def _make_dag_with_deps() -> tuple[DAG, dict]:
    """Create a DAG: planner -> generator -> evaluator."""
    nodes = {
        "planner": DAGNode(
            id="planner", agent_type="planner",
            task_description="Plan", status=NodeStatus.SUCCESS,
            result={"summary": "Plan created"},
            output_artifacts=["plan.md"],
        ),
        "generator": DAGNode(
            id="generator", agent_type="generator",
            task_description="Build", status=NodeStatus.PENDING,
        ),
        "evaluator": DAGNode(
            id="evaluator", agent_type="evaluator",
            task_description="Evaluate", status=NodeStatus.PENDING,
        ),
    }
    edges = [
        DAGEdge(from_node="planner", to_node="generator"),
        DAGEdge(from_node="generator", to_node="evaluator"),
    ]
    return DAG(nodes=nodes, edges=edges), nodes


# ---------------------------------------------------------------------------
# Error pattern detection helpers
# ---------------------------------------------------------------------------

class TestErrorPatternDetection:
    def test_import_error(self):
        assert _has_import_error("ImportError: cannot import foo")
        assert _has_import_error("ModuleNotFoundError: bar")
        assert _has_import_error("cannot import name X")
        assert not _has_import_error("all good")

    def test_type_error(self):
        assert _has_type_error("TypeError: wrong args")
        assert _has_type_error("unexpected keyword argument")
        assert not _has_type_error("no error")

    def test_timeout(self):
        assert _has_timeout("Command timed out after 30s")
        assert _has_timeout("TimeoutExpired")
        assert _has_timeout("TIMEOUT occurred")
        assert not _has_timeout("fast execution")

    def test_coverage_low(self):
        assert _has_coverage_low("coverage below target")
        assert _has_coverage_low("Coverage not verified")
        assert _has_coverage_low("could not be parsed coverage")
        assert not _has_coverage_low("coverage 95%")

    def test_runtime_error(self):
        assert _has_runtime_error("RuntimeError: crash")
        assert _has_runtime_error("AttributeError: no attr")
        assert _has_runtime_error("KeyError: missing key")
        assert not _has_runtime_error("clean run")

    def test_init_import_error(self):
        assert _has_init_import_error(
            "FAIL import_check: mylib/__init__.py — "
            "ImportError: cannot import name 'backend'"
        )
        assert _has_init_import_error(
            "__init__.py import_check failed"
        )
        assert not _has_init_import_error("import_check: main.py")
        assert not _has_init_import_error("__init__.py updated")


# ---------------------------------------------------------------------------
# Basic artifact collection
# ---------------------------------------------------------------------------

class TestArtifactCollection:
    def test_collects_from_successful_deps(self):
        service = ArtifactHandoffService()
        dag, _ = _make_dag_with_deps()

        artifacts = service.collect(dag, "generator")
        assert len(artifacts) == 1
        assert artifacts[0].from_agent == "planner"
        assert artifacts[0].to_agent == "generator"
        assert "plan.md" in artifacts[0].file_paths

    def test_skips_failed_deps(self):
        service = ArtifactHandoffService()
        nodes = {
            "dep": DAGNode(
                id="dep", agent_type="planner",
                task_description="Plan", status=NodeStatus.FAILED,
                result={}, output_artifacts=[],
            ),
            "target": DAGNode(
                id="target", agent_type="generator",
                task_description="Build", status=NodeStatus.PENDING,
            ),
        }
        dag = DAG(nodes=nodes, edges=[DAGEdge(from_node="dep", to_node="target")])
        artifacts = service.collect(dag, "target")
        assert len(artifacts) == 0

    def test_skips_skipped_deps(self):
        service = ArtifactHandoffService()
        nodes = {
            "dep": DAGNode(
                id="dep", agent_type="planner",
                task_description="Plan", status=NodeStatus.SKIPPED,
                result={}, output_artifacts=[],
            ),
            "target": DAGNode(
                id="target", agent_type="generator",
                task_description="Build", status=NodeStatus.PENDING,
            ),
        }
        dag = DAG(nodes=nodes, edges=[DAGEdge(from_node="dep", to_node="target")])
        artifacts = service.collect(dag, "target")
        assert len(artifacts) == 0

    def test_no_deps_returns_empty(self):
        service = ArtifactHandoffService()
        nodes = {
            "solo": DAGNode(
                id="solo", agent_type="generator",
                task_description="Build", status=NodeStatus.PENDING,
            ),
        }
        dag = DAG(nodes=nodes, edges=[])
        artifacts = service.collect(dag, "solo")
        assert len(artifacts) == 0


# ---------------------------------------------------------------------------
# Auto-eval result passing (#145)
# ---------------------------------------------------------------------------

class TestEvalResultPassing:
    def test_passes_eval_result_to_evaluator(self):
        service = ArtifactHandoffService()
        nodes = {
            "gen": DAGNode(
                id="gen", agent_type="generator",
                task_description="Build", status=NodeStatus.SUCCESS,
                result={"summary": "Built"},
                output_artifacts=["main.py"],
                auto_eval_result={
                    "passed": True, "score": 90,
                    "criteria_results": {"tests_pass": True},
                    "feedback": "Good",
                },
            ),
            "eval": DAGNode(
                id="eval", agent_type="evaluator",
                task_description="Evaluate", status=NodeStatus.PENDING,
            ),
        }
        dag = DAG(nodes=nodes, edges=[DAGEdge(from_node="gen", to_node="eval")])
        artifacts = service.collect(dag, "eval")

        # Should have: 1 basic artifact + 1 eval result artifact
        eval_artifacts = [a for a in artifacts if a.from_agent == "auto_evaluator"]
        assert len(eval_artifacts) == 1
        assert "AUTOMATED EVALUATION RESULTS" in eval_artifacts[0].content

    def test_does_not_pass_eval_to_generator(self):
        service = ArtifactHandoffService()
        nodes = {
            "gen1": DAGNode(
                id="gen1", agent_type="generator",
                task_description="Build 1", status=NodeStatus.SUCCESS,
                result={"summary": "Built"},
                output_artifacts=["a.py"],
                auto_eval_result={"passed": True, "score": 90, "feedback": "ok"},
            ),
            "gen2": DAGNode(
                id="gen2", agent_type="generator",
                task_description="Build 2", status=NodeStatus.PENDING,
            ),
        }
        dag = DAG(nodes=nodes, edges=[DAGEdge(from_node="gen1", to_node="gen2")])
        artifacts = service.collect(dag, "gen2")

        eval_artifacts = [a for a in artifacts if a.from_agent == "auto_evaluator"]
        assert len(eval_artifacts) == 0

    def test_does_not_pass_failed_eval(self):
        service = ArtifactHandoffService()
        nodes = {
            "gen": DAGNode(
                id="gen", agent_type="generator",
                task_description="Build", status=NodeStatus.SUCCESS,
                result={"summary": "Built"},
                output_artifacts=["main.py"],
                auto_eval_result={"passed": False, "score": 30, "feedback": "bad"},
            ),
            "eval": DAGNode(
                id="eval", agent_type="evaluator",
                task_description="Evaluate", status=NodeStatus.PENDING,
            ),
        }
        dag = DAG(nodes=nodes, edges=[DAGEdge(from_node="gen", to_node="eval")])
        artifacts = service.collect(dag, "eval")

        eval_artifacts = [a for a in artifacts if a.from_agent == "auto_evaluator"]
        assert len(eval_artifacts) == 0


# ---------------------------------------------------------------------------
# Retry feedback
# ---------------------------------------------------------------------------

class TestRetryFeedback:
    def test_adds_retry_feedback_when_present(self):
        service = ArtifactHandoffService()
        nodes = {
            "target": DAGNode(
                id="target", agent_type="generator",
                task_description="Build", status=NodeStatus.RETRYING,
                eval_feedback="Tests failed: wrong output",
                retry_count=1,
            ),
        }
        dag = DAG(nodes=nodes, edges=[])
        artifacts = service.collect(dag, "target")

        feedback_artifacts = [a for a in artifacts if a.metadata.get("type") == "eval_feedback"]
        assert len(feedback_artifacts) == 1
        assert "RETRY ATTEMPT #1" in feedback_artifacts[0].content
        assert "Tests failed" in feedback_artifacts[0].content

    def test_import_error_guidance(self):
        service = ArtifactHandoffService()
        nodes = {
            "target": DAGNode(
                id="target", agent_type="generator",
                task_description="Build", status=NodeStatus.RETRYING,
                eval_feedback="ImportError: cannot import foo",
                retry_count=2,
            ),
        }
        dag = DAG(nodes=nodes, edges=[])
        artifacts = service.collect(dag, "target")

        feedback_artifacts = [a for a in artifacts if a.metadata.get("type") == "eval_feedback"]
        assert len(feedback_artifacts) == 1
        assert "NAMING MISMATCH" in feedback_artifacts[0].content

    def test_timeout_guidance(self):
        service = ArtifactHandoffService()
        nodes = {
            "target": DAGNode(
                id="target", agent_type="generator",
                task_description="Build", status=NodeStatus.RETRYING,
                eval_feedback="Command timed out after 30s",
                retry_count=1,
            ),
        }
        dag = DAG(nodes=nodes, edges=[])
        artifacts = service.collect(dag, "target")

        feedback = [a for a in artifacts if a.metadata.get("type") == "eval_feedback"][0]
        assert "TIMEOUT DETECTED" in feedback.content

    def test_init_import_error_guidance(self):
        service = ArtifactHandoffService()
        nodes = {
            "target": DAGNode(
                id="target", agent_type="generator",
                task_description="Build", status=NodeStatus.RETRYING,
                eval_feedback=(
                    "FAIL import_check: mylib/__init__.py — "
                    "ImportError: cannot import name 'backend'"
                ),
                retry_count=1,
            ),
        }
        dag = DAG(nodes=nodes, edges=[])
        artifacts = service.collect(dag, "target")

        feedback = [a for a in artifacts if a.metadata.get("type") == "eval_feedback"][0]
        assert "__INIT__.PY IMPORT ERROR" in feedback.content
        assert "MINIMAL" in feedback.content

    def test_lint_error_guidance(self):
        """Lint errors (F811 etc.) trigger targeted fix guidance (#523)."""
        service = ArtifactHandoffService()
        nodes = {
            "target": DAGNode(
                id="target", agent_type="generator",
                task_description="Build", status=NodeStatus.RETRYING,
                eval_feedback=(
                    "Lint: main.py:15: F811 redefinition of unused 'app' "
                    "from line 8"
                ),
                retry_count=1,
            ),
        }
        dag = DAG(nodes=nodes, edges=[])
        artifacts = service.collect(dag, "target")

        feedback = [a for a in artifacts if a.metadata.get("type") == "eval_feedback"]
        assert len(feedback) == 1
        assert "LINT ERROR DETECTED" in feedback[0].content
        assert "F811" in feedback[0].content
        assert "EDIT tool" in feedback[0].content

    def test_no_feedback_when_empty(self):
        service = ArtifactHandoffService()
        nodes = {
            "target": DAGNode(
                id="target", agent_type="generator",
                task_description="Build", status=NodeStatus.PENDING,
            ),
        }
        dag = DAG(nodes=nodes, edges=[])
        artifacts = service.collect(dag, "target")

        feedback_artifacts = [a for a in artifacts if a.metadata.get("type") == "eval_feedback"]
        assert len(feedback_artifacts) == 0


# ---------------------------------------------------------------------------
# Soft dependency warnings
# ---------------------------------------------------------------------------

class TestSoftDepWarning:
    def test_adds_warning_for_failed_soft(self):
        service = ArtifactHandoffService()
        nodes = {
            "soft_dep": DAGNode(
                id="soft_dep", agent_type="planner",
                task_description="Optional plan", status=NodeStatus.FAILED,
                result={}, output_artifacts=[], error="timeout",
            ),
            "target": DAGNode(
                id="target", agent_type="generator",
                task_description="Build", status=NodeStatus.PENDING,
            ),
        }
        dag = DAG(nodes=nodes, edges=[
            DAGEdge(from_node="soft_dep", to_node="target", dependency_type=DependencyType.SOFT),
        ])
        artifacts = service.collect(dag, "target", failed_soft=["soft_dep"])

        warnings = [a for a in artifacts if a.metadata.get("type") == "dependency_warning"]
        assert len(warnings) == 1
        assert "DEPENDENCY WARNING" in warnings[0].content
        assert "soft_dep" in warnings[0].content

    def test_no_warning_when_no_failed_soft(self):
        service = ArtifactHandoffService()
        nodes = {
            "target": DAGNode(
                id="target", agent_type="generator",
                task_description="Build", status=NodeStatus.PENDING,
            ),
        }
        dag = DAG(nodes=nodes, edges=[])
        artifacts = service.collect(dag, "target")

        warnings = [a for a in artifacts if a.metadata.get("type") == "dependency_warning"]
        assert len(warnings) == 0
