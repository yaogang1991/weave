"""
Tests for #320: scope evaluator lint to node-owned files only.

When parallel generator nodes share the same workspace, each node's evaluator
should only lint the files the node was tasked to create — not files from
sibling nodes.
"""

from core.models import SuccessCriterion, CriterionType
from evaluator.engine import EvaluatorEngine


class TestScopeArtifactsToCriteria:
    """Tests for _scope_artifacts_to_criteria static method."""

    def test_no_artifacts_returns_none(self):
        """None artifacts returns None."""
        result = EvaluatorEngine._scope_artifacts_to_criteria(
            None, [], None,
        )
        assert result is None

    def test_empty_artifacts_returns_empty(self):
        """Empty list returns empty list."""
        result = EvaluatorEngine._scope_artifacts_to_criteria(
            [], [], None,
        )
        assert result == []

    def test_no_file_criteria_returns_all(self):
        """Without file_exists/file_pattern criteria, artifacts pass through."""
        criteria = [
            SuccessCriterion(type=CriterionType.LINT, description="lint clean"),
            SuccessCriterion(type=CriterionType.TESTS_PASS, description="tests pass"),
        ]
        artifacts = ["src/a.py", "src/b.py", "src/c.py"]
        result = EvaluatorEngine._scope_artifacts_to_criteria(
            artifacts, criteria, None,
        )
        assert result == artifacts

    def test_file_exists_filters_artifacts(self):
        """Only artifacts matching file_exists criteria are kept."""
        criteria = [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="src/a.py"),
            SuccessCriterion(type=CriterionType.LINT, description="lint clean"),
        ]
        artifacts = ["src/a.py", "src/b.py", "src/c.py"]
        result = EvaluatorEngine._scope_artifacts_to_criteria(
            artifacts, criteria, None,
        )
        assert result == ["src/a.py"]

    def test_multiple_file_exists(self):
        """Multiple file_exists criteria keep matching artifacts."""
        criteria = [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="src/a.py"),
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="src/b.py"),
        ]
        artifacts = ["src/a.py", "src/b.py", "src/c.py"]
        result = EvaluatorEngine._scope_artifacts_to_criteria(
            artifacts, criteria, None,
        )
        assert set(result) == {"src/a.py", "src/b.py"}

    def test_file_pattern_filters_artifacts(self, tmp_path):
        """file_pattern criteria glob against work_dir to find matches."""
        # Create files matching the pattern
        pkg = tmp_path / "schemalib"
        pkg.mkdir()
        (pkg / "fields.py").write_text("x = 1")
        (pkg / "validators.py").write_text("y = 2")
        (pkg / "serialize.py").write_text("z = 3")

        criteria = [
            SuccessCriterion(
                type=CriterionType.FILE_PATTERN,
                pattern="schemalib/fields.py",
            ),
            SuccessCriterion(
                type=CriterionType.FILE_PATTERN,
                pattern="schemalib/validators.py",
            ),
        ]
        artifacts = [
            "schemalib/fields.py",
            "schemalib/validators.py",
            "schemalib/serialize.py",
        ]
        result = EvaluatorEngine._scope_artifacts_to_criteria(
            artifacts, criteria, tmp_path,
        )
        assert "schemalib/fields.py" in result
        assert "schemalib/validators.py" in result
        assert "schemalib/serialize.py" not in result

    def test_cross_node_contamination_prevented(self):
        """Node A's eval doesn't include node B's files (#320 scenario)."""
        # Node A should own coerce.py and serialize.py
        criteria = [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="schemalib/coerce.py"),
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="schemalib/serialize.py"),
        ]
        # But output_artifacts includes ALL files from parallel nodes
        artifacts = [
            "schemalib/coerce.py",
            "schemalib/serialize.py",
            "schemalib/schema.py",     # Node B's file
            "schemalib/validators.py",  # Node B's file
            "schemalib/fields.py",      # Node C's file
            "schemalib/errors.py",      # Node C's file
        ]
        result = EvaluatorEngine._scope_artifacts_to_criteria(
            artifacts, criteria, None,
        )
        assert set(result) == {"schemalib/coerce.py", "schemalib/serialize.py"}

    def test_fallback_when_scoping_removes_everything(self):
        """If scoping removes all artifacts, fall back to original."""
        criteria = [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="nonexistent.py"),
        ]
        artifacts = ["src/a.py", "src/b.py"]
        result = EvaluatorEngine._scope_artifacts_to_criteria(
            artifacts, criteria, None,
        )
        # No match found → fall back to original
        assert result == artifacts

    def test_absolute_path_normalization(self, tmp_path):
        """Absolute artifact paths are normalized for comparison."""
        criteria = [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="src/a.py"),
        ]
        artifacts = [str(tmp_path / "src" / "a.py"), "src/b.py"]
        result = EvaluatorEngine._scope_artifacts_to_criteria(
            artifacts, criteria, tmp_path,
        )
        assert len(result) == 1
        assert "a.py" in result[0]
