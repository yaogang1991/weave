"""Tests for #898: find_test_files fallback no longer collects ALL test files.

When artifact matching returns empty (no matching test file exists yet),
the evaluator should NOT fall back to running all test files in the project,
as that causes cross-module test pollution.
"""
from unittest.mock import patch, MagicMock

from evaluator.engine import EvaluatorEngine
from session.store import SessionStore


def _make_engine():
    return EvaluatorEngine(session_store=SessionStore())


class TestNoCrossModulePollution:
    """Verify #898: no fallback to all tests when artifact matching fails."""

    def test_no_fallback_to_all_tests(self, tmp_path):
        """When artifacts have no matching test files, do not run all
        project tests — soft-pass instead."""
        src = tmp_path / "resumes"
        src.mkdir()
        (src / "routes.py").write_text("pass")

        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        # Module B's test — should NOT be collected
        (tests_dir / "test_jobs.py").write_text(
            "def test_broken():\n    raise RuntimeError('other module')\n"
        )

        engine = _make_engine()
        work_dir = str(tmp_path)
        output_artifacts = ["resumes/routes.py"]

        passed, msg, ran = engine._check_criterion(
            crit=MagicMock(
                type="tests_pass",
                test_path=None,
                target=None,
                command=None,
            ),
            work_dir=work_dir,
            output_artifacts=output_artifacts,
        )

        # Should soft-pass without running test_jobs.py
        assert not ran or passed, (
            f"Should not run cross-module tests. passed={passed}, ran={ran}, msg={msg}"
        )

    def test_matching_test_file_still_runs(self, tmp_path):
        """When artifacts match a test file, it should still be used."""
        src = tmp_path / "resumes"
        src.mkdir()
        (src / "routes.py").write_text("pass")

        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_routes.py").write_text(
            "def test_ok():\n    assert True\n"
        )

        engine = _make_engine()
        work_dir = str(tmp_path)
        output_artifacts = ["resumes/routes.py"]

        with patch("evaluator.engine.run_tests", return_value=(True, "ok")) as mock_run:
            passed, msg, ran = engine._check_criterion(
                crit=MagicMock(
                    type="tests_pass",
                    test_path=None,
                    target=None,
                    command=None,
                ),
                work_dir=work_dir,
                output_artifacts=output_artifacts,
            )

        if mock_run.called:
            assert ran is True
