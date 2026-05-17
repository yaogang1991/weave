"""
Tests for #316: lint-only failures should not block the entire DAG.

When pass_threshold=7.0 (the new default from WeaveConfig), a node with
all hard criteria passing but some soft criteria failing should downgrade
the soft failures to WARNINGs and allow the node to pass.

Uses FILE_EXISTS criteria to avoid subprocess mocking complexity — the
threshold mechanism is the same regardless of criterion type.
"""
import pytest

from core.config import WeaveConfig
from core.models import SuccessCriterion, CriterionType, EvalStatus
from evaluator.engine import EvaluatorEngine
from session.store import SessionStore


@pytest.fixture
def tmp_store(tmp_path):
    return SessionStore(str(tmp_path / "events"))


class TestLintTolerance:
    def test_soft_failure_passes_with_threshold(self, tmp_store, tmp_path):
        """Soft criterion failure (COVERAGE) passes with threshold.

        With pass_threshold=5.0 and 3 criteria (2 pass + 1 soft fail),
        score = 6.67 >= 5.0 → overall pass with PARTIAL_PASS.
        COVERAGE is NOT in HARD_CRITERIA, so threshold can override it.
        """
        (tmp_path / "a.py").write_text("ok", encoding="utf-8")
        (tmp_path / "b.py").write_text("ok", encoding="utf-8")

        engine = EvaluatorEngine(tmp_store, pass_threshold=5.0)

        criteria = [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py",
                             description="file a"),
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="b.py",
                             description="file b"),
            SuccessCriterion(type=CriterionType.COVERAGE,
                             description="test coverage"),
        ]

        result = engine.evaluate_stage(
            "s1", "impl_core", criteria, str(tmp_path),
        )

        # Score = 2/3 * 10 = 6.67 >= 5.0 → pass with PARTIAL_PASS
        assert result.passed
        assert result.eval_status == EvalStatus.PARTIAL_PASS
        assert "WARN" in result.feedback

    def test_hard_criterion_failure_still_fails_with_threshold(self, tmp_store, tmp_path):
        """Hard criterion failure (FILE_EXISTS missing) still fails with threshold."""
        engine = EvaluatorEngine(tmp_store, pass_threshold=5.0)

        criteria = [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="nonexistent.py",
                             description="module exists"),
            SuccessCriterion(type=CriterionType.FILE_PATTERN,
                             pattern="*.py", description="has files"),
        ]

        result = engine.evaluate_stage(
            "s1", "impl_core", criteria, str(tmp_path),
        )

        # FILE_EXISTS is hard → must fail even with threshold
        assert not result.passed
        assert result.eval_status == EvalStatus.FAILED

    def test_config_default_pass_threshold(self):
        """WeaveConfig has default pass_threshold=7.0."""
        cfg = WeaveConfig(llm={"api_key": "test", "model": "test"})
        assert cfg.pass_threshold == 7.0

    def test_config_env_override(self, monkeypatch):
        """WEAVE_PASS_THRESHOLD env var overrides the default."""
        monkeypatch.setenv("WEAVE_PASS_THRESHOLD", "8.5")
        cfg = WeaveConfig(llm={"api_key": "test", "model": "test"})
        assert cfg.pass_threshold == 8.5

    def test_config_explicit_override(self):
        """Explicit pass_threshold in constructor overrides env default."""
        cfg = WeaveConfig(
            llm={"api_key": "test", "model": "test"},
            pass_threshold=9.0,
        )
        assert cfg.pass_threshold == 9.0

    def test_strict_mode_still_available(self, tmp_store, tmp_path):
        """pass_threshold=None still gives strict mode (all must pass)."""
        (tmp_path / "a.py").write_text("ok", encoding="utf-8")

        engine = EvaluatorEngine(tmp_store, pass_threshold=None)

        criteria = [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py",
                             description="file a"),
            SuccessCriterion(type=CriterionType.FILE_PATTERN,
                             pattern="nonexistent_*.py",
                             description="extra files"),
        ]

        result = engine.evaluate_stage(
            "s1", "impl_core", criteria, str(tmp_path),
        )

        # Strict mode: any failure → overall fail
        assert not result.passed
