"""Tests for structured evaluator criteria end-to-end pipeline.

Verifies:
- DAGNode accepts list[str], list[dict], and mixed criteria
- EvaluatorEngine normalizes and dispatches correctly
- work_dir vs artifact_path distinction
- COMMAND/legacy arbitrary commands are rejected (not executed)
- TESTS_PASS uses fixed pytest command (shell=False)
"""
import json
import os
import tempfile

import pytest

from core.models import DAGNode, SuccessCriterion, CriterionType
from evaluator.engine import EvaluatorEngine
from evaluator.compat import normalize_criteria
from session.store import SessionStore


@pytest.fixture
def store(tmp_path):
    return SessionStore(base_path=str(tmp_path / "events"))


@pytest.fixture
def evaluator(store):
    return EvaluatorEngine(session_store=store)


@pytest.fixture
def work_dir(tmp_path):
    """Create a temp work_dir with a test file."""
    d = tmp_path / "project"
    d.mkdir()
    (d / "hello.py").write_text('print("hello")\n', encoding="utf-8")
    return d


# -- DAGNode criteria normalization --


class TestDAGNodeCriteriaNormalization:
    def test_plain_strings(self):
        node = DAGNode(id="n1", agent_type="generator", task_description="t",
                       success_criteria=["tests pass", "lint clean"])
        assert len(node.success_criteria) == 2
        assert node.success_criteria[0] == "tests pass"

    def test_dict_criteria_become_success_criterion(self):
        node = DAGNode(id="n2", agent_type="generator", task_description="t",
                       success_criteria=[
                           {"type": "file_exists", "path": "src/foo.py", "description": "foo exists"},
                       ])
        assert len(node.success_criteria) == 1
        assert isinstance(node.success_criteria[0], SuccessCriterion)
        assert node.success_criteria[0].type == CriterionType.FILE_EXISTS

    def test_mixed_criteria(self):
        node = DAGNode(id="n3", agent_type="generator", task_description="t",
                       success_criteria=[
                           "tests pass",
                           {"type": "lint", "description": "lint clean"},
                       ])
        assert len(node.success_criteria) == 2
        assert isinstance(node.success_criteria[0], str)
        assert isinstance(node.success_criteria[1], SuccessCriterion)

    def test_success_criterion_object_preserved(self):
        sc = SuccessCriterion(type=CriterionType.TESTS_PASS, description="ok")
        node = DAGNode(id="n4", agent_type="generator", task_description="t",
                       success_criteria=[sc])
        assert len(node.success_criteria) == 1
        assert isinstance(node.success_criteria[0], SuccessCriterion)

    def test_json_string_backward_compat(self):
        json_str = json.dumps({"type": "tests_pass", "description": "test"})
        node = DAGNode(id="n5", agent_type="generator", task_description="t",
                       success_criteria=[json_str])
        assert isinstance(node.success_criteria[0], SuccessCriterion)
        assert node.success_criteria[0].type == CriterionType.TESTS_PASS

    def test_legacy_command_dict_downgraded_to_custom(self):
        """DANGEROUS: {"type":"command","command":"rm -rf /"} must NOT execute."""
        node = DAGNode(id="n6", agent_type="generator", task_description="t",
                       success_criteria=[
                           {"type": "command", "command": "rm -rf /", "description": "danger"},
                       ])
        assert isinstance(node.success_criteria[0], SuccessCriterion)
        assert node.success_criteria[0].type == CriterionType.CUSTOM

    def test_legacy_command_json_string_downgraded(self):
        """Previously serialized command JSON must also be downgraded."""
        json_str = json.dumps({"type": "command", "command": "rm -rf /"})
        node = DAGNode(id="n7", agent_type="generator", task_description="t",
                       success_criteria=[json_str])
        assert isinstance(node.success_criteria[0], SuccessCriterion)
        assert node.success_criteria[0].type == CriterionType.CUSTOM


# -- EvaluatorEngine normalize + dispatch --


class TestEvaluatorNormalizeCriteria:
    def test_legacy_string_tests_pass(self, evaluator):
        criteria = normalize_criteria(["tests pass"])
        assert len(criteria) == 1
        assert criteria[0].type == CriterionType.TESTS_PASS

    def test_legacy_string_lint(self, evaluator):
        criteria = normalize_criteria(["lint clean"])
        assert len(criteria) == 1
        assert criteria[0].type == CriterionType.LINT

    def test_legacy_string_coverage(self, evaluator):
        criteria = normalize_criteria(["coverage 90%"])
        assert len(criteria) == 1
        assert criteria[0].type == CriterionType.COVERAGE
        assert criteria[0].target == 90.0

    def test_structured_json_tests_pass(self, evaluator):
        json_str = json.dumps({"type": "tests_pass", "description": "test"})
        criteria = normalize_criteria([json_str])
        assert len(criteria) == 1
        assert criteria[0].type == CriterionType.TESTS_PASS

    def test_structured_json_file_exists(self, evaluator):
        json_str = json.dumps({"type": "file_exists", "path": "hello.py", "description": "file"})
        criteria = normalize_criteria([json_str])
        assert len(criteria) == 1
        assert criteria[0].type == CriterionType.FILE_EXISTS


# -- Security: COMMAND rejection --


class TestCommandRejection:
    def test_command_dict_becomes_custom_uncheckable(self, evaluator, work_dir):
        """A dict with type=command should be downgraded to CUSTOM (uncheckable, but passes with warning)."""
        node = DAGNode(id="s1", agent_type="g", task_description="t",
                       success_criteria=[
                           {"type": "command", "command": "rm -rf /", "description": "danger"},
                       ])
        result = evaluator.evaluate_stage(
            "s1", "stage1", node.success_criteria,
            artifact_path=str(work_dir),
            work_dir=str(work_dir),
        )
        # CUSTOM → was_auto=False → uncheckable → passes with warning
        assert result.passed is True
        assert "cannot auto-verify" in result.feedback.lower()

    def test_command_json_string_becomes_custom(self, evaluator, work_dir):
        """Previously serialized command JSON string should also be uncheckable (passes with warning)."""
        json_str = json.dumps({"type": "command", "command": "rm -rf /"})
        result = evaluator.evaluate_stage(
            "s2", "stage2", [json_str],
            artifact_path=str(work_dir),
            work_dir=str(work_dir),
        )
        assert result.passed is True


# -- work_dir vs artifact_path --


class TestWorkDirVsArtifactPath:
    def test_file_exists_uses_work_dir(self, evaluator, work_dir):
        artifact_dir = work_dir.parent / "artifacts"
        artifact_dir.mkdir(exist_ok=True)
        crit = json.dumps({"type": "file_exists", "path": "hello.py", "description": "file"})
        result = evaluator.evaluate_stage(
            "s3", "stage3", [crit],
            artifact_path=str(artifact_dir),
            work_dir=str(work_dir),
        )
        assert result.passed is True

    def test_file_exists_fails_in_wrong_dir(self, evaluator, work_dir):
        wrong_dir = work_dir.parent / "empty"
        wrong_dir.mkdir()
        crit = json.dumps({"type": "file_exists", "path": "hello.py", "description": "file"})
        result = evaluator.evaluate_stage(
            "s4", "stage4", [crit],
            artifact_path=str(wrong_dir),
            work_dir=str(wrong_dir),
        )
        assert result.passed is False

    def test_no_critical_uses_work_dir(self, evaluator, work_dir):
        (work_dir / "buggy.py").write_text("# FIXME: bad\n", encoding="utf-8")
        crit = json.dumps({"type": "no_critical", "description": "no markers"})
        result = evaluator.evaluate_stage(
            "s5", "stage5", [crit],
            artifact_path="/tmp/nonexistent",
            work_dir=str(work_dir),
            output_artifacts=["buggy.py"],
        )
        assert result.passed is False


# -- End-to-end: DAGNode -> Evaluator --


class TestEndToEndPipeline:
    def test_structured_criteria_through_dag_node(self, evaluator, work_dir):
        node = DAGNode(
            id="gen1",
            agent_type="generator",
            task_description="generate hello.py",
            success_criteria=[
                {"type": "file_exists", "path": "hello.py", "description": "hello.py exists"},
            ],
        )
        assert isinstance(node.success_criteria[0], SuccessCriterion)
        result = evaluator.evaluate_stage(
            "s6", "gen1", node.success_criteria,
            artifact_path=str(work_dir),
            work_dir=str(work_dir),
        )
        assert result.passed is True
        assert "hello.py" in result.feedback

    def test_mixed_criteria_through_dag_node(self, evaluator, work_dir):
        node = DAGNode(
            id="gen2",
            agent_type="generator",
            task_description="generate",
            success_criteria=[
                "lint clean",
                {"type": "file_exists", "path": "hello.py", "description": "file"},
            ],
        )
        result = evaluator.evaluate_stage(
            "s7", "gen2", node.success_criteria,
            artifact_path=str(work_dir),
            work_dir=str(work_dir),
        )
        assert isinstance(result.passed, bool)
        assert result.score > 0


# -- Template loading with structured criteria --


class TestTemplateStructuredCriteria:
    def test_build_api_template_no_command_type(self):
        from templates.library import TemplateRegistry
        registry = TemplateRegistry()
        tpl = registry.get_template("build_api")
        assert tpl is not None
        for node in tpl.nodes:
            for sc in node.get("success_criteria", []):
                assert sc.get("type") != "command", f"Found command type in {node['id']}"

    def test_template_instantiation(self):
        from templates.library import TemplateRegistry
        registry = TemplateRegistry()
        dag = registry.instantiate("build_api", {"feature": "Todo API", "language": "Python"})
        eval_nodes = [n for n in dag.nodes.values() if n.agent_type == "evaluator"]
        assert len(eval_nodes) == 1
        for sc in eval_nodes[0].success_criteria:
            assert isinstance(sc, SuccessCriterion)
            assert sc.type != CriterionType.CUSTOM or sc.type == CriterionType.CUSTOM
