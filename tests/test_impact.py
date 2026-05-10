"""
Tests for M3.5 Impact Analysis System.

Covers: ImpactScope/VerificationResult models, DependencyGraph,
ImpactPredictor, ChangeVerifier, CLI commands, and integration.
"""

import json
import pytest
from unittest.mock import MagicMock

from core.models import (
    ImpactScope, ImpactRiskLevel, VerificationResult, EventType,
)
from core.config import ImpactConfig
from analysis.dependency_graph import DependencyGraph
from analysis.impact_predictor import ImpactPredictor
from analysis.change_verifier import ChangeVerifier


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def sample_project(tmp_path):
    """Create a minimal Python project for testing."""
    # Create package structure
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "models.py").write_text(
        "from app.database import DB\n"
        "class User:\n    pass\n"
    )
    (tmp_path / "app" / "database.py").write_text(
        "import os\n"
        "class DB:\n    pass\n"
    )
    (tmp_path / "app" / "api.py").write_text(
        "from app.models import User\n"
        "from app.database import DB\n"
    )
    (tmp_path / "app" / "utils.py").write_text(
        "import json\n"
    )
    return tmp_path


@pytest.fixture
def dep_graph(sample_project):
    graph = DependencyGraph(str(sample_project))
    graph.build()
    return graph


@pytest.fixture
def predictor():
    return ImpactPredictor()


@pytest.fixture
def verifier(sample_project):
    return ChangeVerifier(
        project_path=str(sample_project),
        coverage_threshold=0.7,
    )


# =============================================================================
# TestModels
# =============================================================================


class TestModels:
    def test_impact_scope_defaults(self):
        scope = ImpactScope(requirement="Fix bug")
        assert scope.id.startswith("imp_")
        assert scope.risk_level == ImpactRiskLevel.MEDIUM
        assert scope.confidence == 0.0
        assert scope.predicted_files == []
        assert scope.created_at.tzinfo is not None

    def test_impact_scope_serialization(self):
        scope = ImpactScope(
            requirement="Add auth",
            predicted_files=["app/auth.py"],
            risk_level=ImpactRiskLevel.HIGH,
            confidence=0.85,
        )
        data = scope.model_dump(mode="json")
        restored = ImpactScope(**data)
        assert restored.id == scope.id
        assert restored.risk_level == ImpactRiskLevel.HIGH
        assert restored.confidence == 0.85

    def test_verification_result_defaults(self):
        vr = VerificationResult(impact_scope_id="imp_123")
        assert vr.coverage == 0.0
        assert vr.passes is False  # fail-closed default
        assert vr.covered_files == []

    def test_verification_result_serialization(self):
        vr = VerificationResult(
            impact_scope_id="imp_123",
            covered_files=["a.py"],
            unexpected_files=["b.py"],
            coverage=0.5,
            passes=False,
        )
        data = vr.model_dump(mode="json")
        restored = VerificationResult(**data)
        assert restored.coverage == 0.5
        assert not restored.passes

    def test_impact_risk_level_values(self):
        assert ImpactRiskLevel.LOW.value == "low"
        assert ImpactRiskLevel.MEDIUM.value == "medium"
        assert ImpactRiskLevel.HIGH.value == "high"
        assert ImpactRiskLevel.CRITICAL.value == "critical"

    def test_event_types_impact(self):
        assert EventType.IMPACT_PREDICTED == "impact.predicted"
        assert EventType.IMPACT_VERIFIED == "impact.verified"
        assert EventType.IMPACT_MISMATCH == "impact.mismatch"
        assert EventType.IMPACT_LEARNED == "impact.learned"


# =============================================================================
# TestDependencyGraph
# =============================================================================


class TestDependencyGraph:
    def test_build_empty_project(self, tmp_path):
        graph = DependencyGraph(str(tmp_path))
        result = graph.build()
        assert result == {}

    def test_build_single_file(self, tmp_path):
        (tmp_path / "main.py").write_text("x = 1")
        graph = DependencyGraph(str(tmp_path))
        graph.build()
        assert "main.py" in graph._graph
        assert graph._graph["main.py"] == set()

    def test_build_with_imports(self, sample_project):
        graph = DependencyGraph(str(sample_project))
        graph.build()
        api_deps = graph._graph.get("app/api.py", set())
        assert "app/models.py" in api_deps or "app/database.py" in api_deps

    def test_get_dependents(self, dep_graph):
        dependents = dep_graph.get_dependents("app/models.py")
        assert isinstance(dependents, set)
        # api.py depends on models.py (directly or transitively)
        assert "app/api.py" in dependents

    def test_get_dependencies(self, dep_graph):
        deps = dep_graph.get_dependencies("app/api.py")
        assert isinstance(deps, set)
        assert "app/models.py" in deps

    def test_get_module_files(self, dep_graph):
        files = dep_graph.get_module_files("app.models")
        assert "app/models.py" in files

    def test_to_dict(self, dep_graph):
        d = dep_graph.to_dict()
        assert isinstance(d, dict)
        assert all(isinstance(v, list) for v in d.values())

    def test_ignores_venv(self, sample_project):
        venv_dir = sample_project / ".venv"
        venv_dir.mkdir()
        (venv_dir / "site.py").write_text("import os")
        graph = DependencyGraph(str(sample_project))
        graph.build()
        assert ".venv/site.py" not in graph._graph

    def test_path_to_module(self, dep_graph):
        assert dep_graph._path_to_module("app/models.py") == "app.models"
        assert dep_graph._path_to_module("app/__init__.py") == "app"


# =============================================================================
# TestImpactPredictor
# =============================================================================


class TestImpactPredictor:
    @pytest.mark.asyncio
    async def test_predict_static(self, predictor, sample_project):
        scope = predictor.predict_static(
            "Add database migration",
            str(sample_project),
        )
        assert isinstance(scope, ImpactScope)
        assert scope.requirement == "Add database migration"
        assert isinstance(scope.predicted_files, list)

    @pytest.mark.asyncio
    async def test_predict_keyword_match(self, predictor, sample_project):
        scope = predictor.predict_static(
            "Fix bug in database",
            str(sample_project),
        )
        assert any("database" in f for f in scope.predicted_files)

    @pytest.mark.asyncio
    async def test_predict_no_match(self, predictor, sample_project):
        scope = predictor.predict_static(
            "Add quantum computing module",
            str(sample_project),
        )
        assert isinstance(scope.predicted_files, list)
        assert scope.risk_level == ImpactRiskLevel.LOW

    @pytest.mark.asyncio
    async def test_predict_risk_levels(self, predictor, sample_project):
        # Low risk: no matches
        scope = predictor.predict_static("xyzzy", str(sample_project))
        assert scope.risk_level == ImpactRiskLevel.LOW

    def test_compute_risk_level(self, predictor):
        assert predictor._compute_risk_level(0, 0) == ImpactRiskLevel.LOW
        assert predictor._compute_risk_level(1, 1) == ImpactRiskLevel.LOW
        assert predictor._compute_risk_level(5, 2) == ImpactRiskLevel.MEDIUM
        assert predictor._compute_risk_level(15, 4) == ImpactRiskLevel.HIGH
        assert predictor._compute_risk_level(50, 10) == ImpactRiskLevel.CRITICAL

    def test_keyword_match_files(self, predictor, sample_project):
        matches = predictor._keyword_match_files(
            "Fix bug in models", str(sample_project),
        )
        assert "app/models.py" in matches

    def test_historical_prediction_no_memory(self, predictor):
        result = predictor._get_historical_prediction("test")
        assert result is None

    def test_historical_prediction_with_memory(self):
        mock_mm = MagicMock()
        mock_mm.store.search.return_value = []
        predictor = ImpactPredictor(memory_manager=mock_mm)
        result = predictor._get_historical_prediction("test")
        assert result is None  # No matching entries

    @pytest.mark.asyncio
    async def test_predict_uses_static(self, predictor, sample_project):
        scope = await predictor.predict(
            "Refactor API", str(sample_project),
        )
        assert isinstance(scope, ImpactScope)
        assert "Static analysis" in scope.reasoning


# =============================================================================
# TestChangeVerifier
# =============================================================================


class TestChangeVerifier:
    def test_capture_snapshot(self, verifier, sample_project):
        snapshot = verifier.capture_snapshot()
        assert isinstance(snapshot, dict)
        assert len(snapshot) > 0
        assert "app/models.py" in snapshot

    def test_verify_no_changes(self, verifier):
        snapshot = {"a.py": 1000.0, "b.py": 2000.0}
        scope = ImpactScope(
            requirement="test",
            predicted_files=["a.py"],
        )
        result = verifier.verify(scope, snapshot, snapshot)
        assert result.coverage == 0.0
        assert result.actual_changed_files == []

    def test_verify_all_predicted(self, verifier):
        before = {"a.py": 1000.0, "b.py": 2000.0}
        after = {"a.py": 3000.0, "b.py": 2000.0}
        scope = ImpactScope(
            requirement="test",
            predicted_files=["a.py"],
        )
        result = verifier.verify(scope, before, after)
        assert "a.py" in result.covered_files
        assert result.coverage == 1.0

    def test_verify_unexpected_changes(self, verifier):
        before = {"a.py": 1000.0}
        after = {"a.py": 2000.0, "b.py": 3000.0}
        scope = ImpactScope(
            requirement="test",
            predicted_files=["a.py"],
        )
        result = verifier.verify(scope, before, after)
        assert "b.py" in result.unexpected_files
        assert result.coverage == 0.5

    def test_verify_passes_threshold(self, verifier):
        before = {"a.py": 1000.0, "b.py": 2000.0}
        after = {"a.py": 3000.0, "b.py": 4000.0}
        scope = ImpactScope(
            requirement="test",
            predicted_files=["a.py", "b.py"],
        )
        result = verifier.verify(scope, before, after)
        assert result.passes  # coverage=1.0 >= 0.7

    def test_verify_fails_threshold(self):
        v = ChangeVerifier(project_path="/tmp", coverage_threshold=0.9)
        before = {"a.py": 1000.0, "b.py": 2000.0, "c.py": 3000.0}
        after = {"a.py": 4000.0, "b.py": 5000.0, "c.py": 6000.0}
        scope = ImpactScope(
            requirement="test",
            predicted_files=["a.py"],
        )
        result = v.verify(scope, before, after)
        assert not result.passes  # coverage=0.33 < 0.9

    def test_verify_missed_files(self, verifier):
        before = {"a.py": 1000.0}
        after = {"a.py": 1000.0}  # No changes
        scope = ImpactScope(
            requirement="test",
            predicted_files=["a.py", "b.py"],
        )
        result = verifier.verify(scope, before, after)
        assert "a.py" in result.missed_files or "b.py" in result.missed_files

    def test_get_changed_files_new_file(self, verifier):
        before = {"a.py": 1000.0}
        after = {"a.py": 1000.0, "new.py": 5000.0}
        changed = verifier.get_changed_files(before, after)
        assert "new.py" in changed
        assert "a.py" not in changed

    def test_get_changed_files_deleted(self, verifier):
        before = {"a.py": 1000.0, "b.py": 2000.0}
        after = {"a.py": 1000.0}
        changed = verifier.get_changed_files(before, after)
        assert "b.py" in changed

    def test_get_changed_files_modified(self, verifier):
        before = {"a.py": 1000.0}
        after = {"a.py": 2000.0}
        changed = verifier.get_changed_files(before, after)
        assert "a.py" in changed


# =============================================================================
# TestImpactConfig
# =============================================================================


class TestImpactConfig:
    def test_defaults(self):
        config = ImpactConfig()
        assert config.enabled is True
        assert config.coverage_threshold == 0.7
        assert config.max_predicted_files == 50
        assert config.confidence_threshold == 0.5

    def test_custom_values(self):
        config = ImpactConfig(
            enabled=False,
            coverage_threshold=0.9,
            max_predicted_files=100,
        )
        assert not config.enabled
        assert config.coverage_threshold == 0.9


# =============================================================================
# TestIntegration
# =============================================================================


class TestImpactIntegration:
    @pytest.mark.asyncio
    async def test_predict_then_verify(self, sample_project):
        predictor = ImpactPredictor()
        scope = await predictor.predict(
            "Fix bug in database",
            str(sample_project),
        )
        assert isinstance(scope, ImpactScope)

        verifier = ChangeVerifier(str(sample_project))
        before = verifier.capture_snapshot()

        # Simulate a change to database.py
        db_file = sample_project / "app" / "database.py"
        db_file.write_text("class DB:\n    NEW = True\n")

        after = verifier.capture_snapshot()
        result = verifier.verify(scope, before, after)
        assert isinstance(result, VerificationResult)
        assert result.actual_changed_files != []

    def test_dependency_graph_with_predictor(self, sample_project):
        graph = DependencyGraph(str(sample_project))
        graph.build()
        predictor = ImpactPredictor()

        # Use the graph to expand predictions
        matched = predictor._keyword_match_files(
            "Fix database", str(sample_project),
        )
        expanded = predictor._expand_with_dependencies(matched, graph)
        # Should include api.py since it depends on database.py
        assert len(expanded) >= len(matched)


# =============================================================================
# TestCLI
# =============================================================================


class TestImpactCLI:
    def test_impact_predict_command(self):
        import subprocess
        result = subprocess.run(
            [".venv/bin/python", "main.py", "impact-predict",
             "Fix bug", "--project", "."],
            capture_output=True, text=True,
            cwd="/Users/yaogang/project/harness",
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "id" in data
        assert data["risk_level"] in ("low", "medium", "high", "critical")

    def test_impact_graph_command(self):
        import subprocess
        result = subprocess.run(
            [".venv/bin/python", "main.py", "impact-graph",
             "--project", "."],
            capture_output=True, text=True,
            cwd="/Users/yaogang/project/harness",
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "files" in data
        assert data["files"] > 0

    def test_impact_history_command(self):
        import subprocess
        result = subprocess.run(
            [".venv/bin/python", "main.py", "impact-history"],
            capture_output=True, text=True,
            cwd="/Users/yaogang/project/harness",
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "count" in data
