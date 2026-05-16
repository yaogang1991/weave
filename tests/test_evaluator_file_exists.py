"""
Tests for #158: evaluator file_exists must verify files on disk,
not trust agent-reported output_artifacts.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.models import CriterionType, SuccessCriterion
from evaluator.engine import EvaluatorEngine


class TestFileExistsDiskVerification:
    """file_exists criterion must check disk, not just output_artifacts."""

    @pytest.fixture
    def engine(self):
        store = MagicMock()
        store.emit_event = MagicMock()
        return EvaluatorEngine(store)

    @pytest.fixture
    def tmp_work_dir(self, tmp_path: Path) -> Path:
        return tmp_path

    def test_pass_when_file_exists_on_disk(self, engine, tmp_work_dir):
        """File exists on disk → PASS."""
        f = tmp_work_dir / "hello.py"
        f.write_text("print('hi')", encoding="utf-8")

        crit = SuccessCriterion(
            type=CriterionType.FILE_EXISTS,
            path="hello.py",
        )
        passed, msg, auto = engine._check_criterion(
            crit, str(tmp_work_dir), output_artifacts=["hello.py"],
        )
        assert passed is True
        assert "verified on disk" in msg.lower()
        assert auto is True

    def test_fail_when_agent_reports_but_file_missing(
        self, engine, tmp_work_dir,
    ):
        """Agent claims file exists but disk disagrees → FAIL (#158)."""
        crit = SuccessCriterion(
            type=CriterionType.FILE_EXISTS,
            path="bogus.py",
        )
        passed, msg, auto = engine._check_criterion(
            crit, str(tmp_work_dir), output_artifacts=["bogus.py"],
        )
        assert passed is False
        assert "missing" in msg.lower()
        assert auto is True

    def test_empty_file_counts_as_existing(self, engine, tmp_work_dir):
        """Empty (0-byte) file counts as existing (e.g. __init__.py)."""
        f = tmp_work_dir / "empty.py"
        f.write_text("", encoding="utf-8")

        crit = SuccessCriterion(
            type=CriterionType.FILE_EXISTS,
            path="empty.py",
        )
        passed, msg, auto = engine._check_criterion(
            crit, str(tmp_work_dir), output_artifacts=["empty.py"],
        )
        assert passed is True

    def test_pass_with_loose_match(self, engine, tmp_work_dir):
        """Exact path missing but stem glob finds file → PASS."""
        f = tmp_work_dir / "deep" / "my_module.py"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x = 1\n", encoding="utf-8")

        crit = SuccessCriterion(
            type=CriterionType.FILE_EXISTS,
            path="my_module.py",
        )
        passed, msg, auto = engine._check_criterion(
            crit, str(tmp_work_dir), output_artifacts=["my_module.py"],
        )
        assert passed is True
        assert "verified on disk" in msg.lower()

    def test_pass_with_only_planner_path(self, engine, tmp_work_dir):
        """No output_artifacts but planner path exists on disk → PASS."""
        f = tmp_work_dir / "planned.py"
        f.write_text("x = 1\n", encoding="utf-8")

        crit = SuccessCriterion(
            type=CriterionType.FILE_EXISTS,
            path="planned.py",
        )
        passed, msg, auto = engine._check_criterion(
            crit, str(tmp_work_dir), output_artifacts=[],
        )
        assert passed is True
        assert "verified on disk" in msg.lower()

    def test_fail_both_missing(self, engine, tmp_work_dir):
        """Neither planner path nor output_artifacts exist → FAIL."""
        crit = SuccessCriterion(
            type=CriterionType.FILE_EXISTS,
            path="nowhere.py",
        )
        passed, msg, auto = engine._check_criterion(
            crit, str(tmp_work_dir), output_artifacts=[],
        )
        assert passed is False

    def test_verifies_all_candidates(self, engine, tmp_work_dir):
        """Multiple files: all must exist."""
        (tmp_work_dir / "a.py").write_text("a", encoding="utf-8")
        # b.py is missing

        crit = SuccessCriterion(
            type=CriterionType.FILE_EXISTS,
            path="a.py,b.py",
        )
        passed, msg, auto = engine._check_criterion(
            crit, str(tmp_work_dir),
            output_artifacts=["a.py", "b.py"],
        )
        assert passed is False
        assert "b.py" in msg

    def test_no_candidates_passes_by_default(self, engine, tmp_work_dir):
        """No files specified and no artifacts → pass vacuously (None)."""
        crit = SuccessCriterion(type=CriterionType.FILE_EXISTS)
        passed, msg, auto = engine._check_criterion(
            crit, str(tmp_work_dir), output_artifacts=None,
        )
        assert passed is True
        assert auto is True

    def test_empty_artifacts_fails(self, engine, tmp_work_dir):
        """Empty output_artifacts [] → fail (#372 zero-output guard)."""
        crit = SuccessCriterion(type=CriterionType.FILE_EXISTS)
        passed, msg, auto = engine._check_criterion(
            crit, str(tmp_work_dir), output_artifacts=[],
        )
        assert passed is False


class TestAgentArtifactTrackingDiskCheck:
    """AgentWorker._track_artifact should verify file on disk."""

    def test_tracks_existing_file(self, tmp_path: Path):
        from agent.worker import AgentWorker
        from core.config import LLMConfig

        store = MagicMock()
        store.emit_event = MagicMock()
        worker = AgentWorker(
            LLMConfig(provider="openai", api_key="x", model="gpt-4"),
            store,
        )

        f = tmp_path / "real.py"
        f.write_text("x = 1\n", encoding="utf-8")
        worker._track_artifact("write", {"file_path": str(f)})
        assert str(f) in worker.artifacts

    def test_ignores_missing_file(self, tmp_path: Path):
        from agent.worker import AgentWorker
        from core.config import LLMConfig

        store = MagicMock()
        store.emit_event = MagicMock()
        worker = AgentWorker(
            LLMConfig(provider="openai", api_key="x", model="gpt-4"),
            store,
        )

        missing = tmp_path / "missing.py"
        worker._track_artifact("write", {"file_path": str(missing)})
        assert str(missing) not in worker.artifacts

    def test_ignores_empty_file(self, tmp_path: Path):
        from agent.worker import AgentWorker
        from core.config import LLMConfig

        store = MagicMock()
        store.emit_event = MagicMock()
        worker = AgentWorker(
            LLMConfig(provider="openai", api_key="x", model="gpt-4"),
            store,
        )

        empty = tmp_path / "empty.py"
        empty.write_text("", encoding="utf-8")
        worker._track_artifact("write", {"file_path": str(empty)})
        assert str(empty) not in worker.artifacts
