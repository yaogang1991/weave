"""
Tests for #285: PlanValidator renaming sync with evaluator criteria.

Covers:
- PlanValidator.build_rename_map produces correct mappings
- Orchestrator._apply_rename_map rewrites criterion paths
- EvaluatorEngine._try_stdlib_rename finds prefixed alternatives
- EvaluatorEngine FILE_EXISTS / FILE_PATTERN fallback with stdlib prefix
"""
from unittest.mock import MagicMock

from core.models import (
    DAG,
    DAGNode,
    CriterionType,
    SuccessCriterion,
)
from orchestrator.plan_validator import PlanValidator
from orchestrator.intelligent_orchestrator import IntelligentOrchestrator


# ---------------------------------------------------------------------------
# PlanValidator.rename_map
# ---------------------------------------------------------------------------

class TestPlanValidatorRenameMap:
    def test_produces_rename_map_on_stdlib_conflict(self):
        pv = PlanValidator()
        plan_data = {
            "nodes": [
                {"id": "n1", "task": 'create a module called "types" for data'},
                {"id": "n2", "task": "create utils library"},
            ],
            "edges": [],
        }
        pv.validate(plan_data)
        assert "types" in pv.rename_map
        assert pv.rename_map["types"] == "app_types"

    def test_produces_rename_for_types(self):
        pv = PlanValidator()
        plan_data = {
            "nodes": [
                {"id": "n1", "task": 'build a package called "types"'},
            ],
            "edges": [],
        }
        pv.validate(plan_data)
        assert "types" in pv.rename_map
        assert pv.rename_map["types"] == "app_types"

    def test_no_rename_when_no_conflict(self):
        pv = PlanValidator()
        plan_data = {
            "nodes": [
                {"id": "n1", "task": "create user_auth module"},
            ],
            "edges": [],
        }
        pv.validate(plan_data)
        assert pv.rename_map == {}


# ---------------------------------------------------------------------------
# Orchestrator._apply_rename_map
# ---------------------------------------------------------------------------

class TestApplyRenameMap:
    def test_rewrites_file_pattern_criterion(self):
        dag = DAG(reasoning="test")
        node = DAGNode(
            id="gen1",
            agent_type="generator",
            task_description="build models",
            success_criteria=[
                SuccessCriterion(
                    type=CriterionType.FILE_PATTERN,
                    pattern="exam_app/models/*.py",
                ),
            ],
        )
        dag.add_node(node)

        IntelligentOrchestrator._apply_rename_map(dag, {"models": "app_models"})

        crit = dag.nodes["gen1"].success_criteria[0]
        assert isinstance(crit, SuccessCriterion)
        assert crit.pattern == "exam_app/app_models/*.py"

    def test_rewrites_file_exists_criterion(self):
        dag = DAG(reasoning="test")
        node = DAGNode(
            id="gen1",
            agent_type="generator",
            task_description="build models",
            success_criteria=[
                SuccessCriterion(
                    type=CriterionType.FILE_EXISTS,
                    path="exam_app/models/__init__.py",
                ),
            ],
        )
        dag.add_node(node)

        IntelligentOrchestrator._apply_rename_map(dag, {"models": "app_models"})

        crit = dag.nodes["gen1"].success_criteria[0]
        assert isinstance(crit, SuccessCriterion)
        assert crit.path == "exam_app/app_models/__init__.py"

    def test_rewrites_string_criteria(self):
        dag = DAG(reasoning="test")
        node = DAGNode(
            id="gen1",
            agent_type="generator",
            task_description="build types",
            success_criteria=["file_exists: exam_app/types/base.py"],
        )
        dag.add_node(node)

        IntelligentOrchestrator._apply_rename_map(dag, {"types": "app_types"})

        assert dag.nodes["gen1"].success_criteria[0] == "file_exists: exam_app/app_types/base.py"

    def test_no_rewrite_unrelated_criteria(self):
        dag = DAG(reasoning="test")
        node = DAGNode(
            id="gen1",
            agent_type="generator",
            task_description="build utils",
            success_criteria=[
                SuccessCriterion(
                    type=CriterionType.FILE_EXISTS,
                    path="exam_app/utils/helpers.py",
                ),
            ],
        )
        dag.add_node(node)

        IntelligentOrchestrator._apply_rename_map(dag, {"models": "app_models"})

        crit = dag.nodes["gen1"].success_criteria[0]
        assert crit.path == "exam_app/utils/helpers.py"

    def test_updates_task_description_with_renamed_files(self):
        """Task description should be updated to match renamed criteria (#422)."""
        dag = DAG(reasoning="test")
        node = DAGNode(
            id="gen1",
            agent_type="generator",
            task_description="implement convertlib/numbers.py module",
            success_criteria=[
                SuccessCriterion(
                    type=CriterionType.FILE_EXISTS,
                    path="convertlib/numbers.py",
                ),
            ],
        )
        dag.add_node(node)

        IntelligentOrchestrator._apply_rename_map(dag, {"numbers": "app_numbers"})

        # Task description should now reference app_numbers.py (prefixed)
        # Note: "numbers.py" is a substring of "app_numbers.py", so we check
        # that the full expected form is present
        assert "convertlib/app_numbers.py" in dag.nodes["gen1"].task_description
        # Criteria should also be updated
        crit = dag.nodes["gen1"].success_criteria[0]
        assert isinstance(crit, SuccessCriterion)
        assert crit.path == "convertlib/app_numbers.py"

    def test_does_not_double_rename_already_prefixed(self):
        """Already-prefixed names like app_numbers.py should not be renamed again."""
        dag = DAG(reasoning="test")
        node = DAGNode(
            id="gen1",
            agent_type="generator",
            task_description="implement app_numbers.py module",
            success_criteria=[],
        )
        dag.add_node(node)

        IntelligentOrchestrator._apply_rename_map(dag, {"numbers": "app_numbers"})

        # Should NOT become "app_app_numbers.py"
        assert "app_app_numbers" not in dag.nodes["gen1"].task_description
        assert "app_numbers.py" in dag.nodes["gen1"].task_description


# ---------------------------------------------------------------------------
# EvaluatorEngine._try_stdlib_rename
# ---------------------------------------------------------------------------

class TestEvaluatorStdlibRenameFallback:
    def test_finds_prefixed_directory(self, tmp_path):
        from evaluator.checkers.file_exists import FileExistsChecker

        # Create the renamed directory with files
        (tmp_path / "exam_app" / "app_models").mkdir(parents=True)
        (tmp_path / "exam_app" / "app_models" / "user.py").write_text("x = 1")

        result = FileExistsChecker._try_stdlib_rename("exam_app/models/*.py", tmp_path)
        assert result is not None
        assert "app_models" in result

    def test_returns_none_when_nothing_matches(self, tmp_path):
        from evaluator.checkers.file_exists import FileExistsChecker

        result = FileExistsChecker._try_stdlib_rename("nonexistent/models/*.py", tmp_path)
        assert result is None

    def test_file_pattern_fallback_passes(self, tmp_path):
        """FILE_PATTERN with renamed directory should pass via fallback."""
        from evaluator.checkers.file_exists import FileExistsChecker
        from evaluator.models import EvaluationContext

        (tmp_path / "exam_app" / "app_models").mkdir(parents=True)
        (tmp_path / "exam_app" / "app_models" / "user.py").write_text("x = 1")
        (tmp_path / "exam_app" / "app_models" / "role.py").write_text("y = 2")

        checker = FileExistsChecker()

        crit = SuccessCriterion(
            type=CriterionType.FILE_PATTERN,
            pattern="exam_app/models/*.py",
        )
        context = EvaluationContext(
            work_dir=tmp_path,
            artifacts=None,
            session_store=None,
        )
        result = checker._check_file_pattern(crit, context)
        assert result.passed
        assert "app_models" in result.message

    def test_file_exists_fallback_passes(self, tmp_path):
        """FILE_EXISTS with renamed directory should pass via fallback."""
        from evaluator.engine import EvaluatorEngine
        from session.store import SessionStore

        (tmp_path / "exam_app" / "app_models").mkdir(parents=True)
        (tmp_path / "exam_app" / "app_models" / "user.py").write_text("x = 1")

        store = MagicMock(spec=SessionStore)
        engine = EvaluatorEngine(session_store=store)

        crit = SuccessCriterion(
            type=CriterionType.FILE_EXISTS,
            path="exam_app/models/user.py",
        )
        passed, msg, _ = engine._check_criterion(crit, str(tmp_path))
        assert passed
        assert "app_models" in msg or "verified" in msg.lower()
