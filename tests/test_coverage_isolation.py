"""Test coverage file isolation for parallel DAG nodes (#260).

Verifies that each evaluation gets a unique COVERAGE_FILE env variable,
preventing parallel nodes from corrupting each other's coverage data.
"""

import os
import sys
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evaluator.engine import EvaluatorEngine
from core.models import (
    CriterionType,
    EvaluationResult,
    SuccessCriterion,
)
from session.store import SessionStore


@pytest.fixture
def engine(tmp_path):
    store = SessionStore(str(tmp_path / "events"))
    return EvaluatorEngine(store)


@pytest.fixture
def work_dir(tmp_path):
    return tmp_path


def _make_criteria(**overrides):
    defaults = {
        "type": CriterionType.TESTS_PASS,
        "description": "Tests pass",
    }
    defaults.update(overrides)
    return [SuccessCriterion(**defaults)]


class TestCoverageFileIsolation:
    """Each parallel DAG node should get an isolated COVERAGE_FILE."""

    def test_isolated_env_has_coverage_file(self, engine):
        """_isolated_env returns env with unique COVERAGE_FILE when eval_id provided."""
        env = engine._isolated_env("sess-1_impl_core")
        assert "COVERAGE_FILE" in env
        assert env["COVERAGE_FILE"] == ".coverage.sess-1_impl_core"

    def test_isolated_env_preserves_existing_env(self, engine):
        """_isolated_env preserves existing environment variables."""
        env = engine._isolated_env("test")
        assert "PATH" in env

    def test_isolated_env_empty_eval_id(self, engine):
        """When eval_id is empty, no COVERAGE_FILE is set."""
        env = engine._isolated_env("")
        assert "COVERAGE_FILE" not in env

    def test_run_tests_uses_isolated_env(self, engine, tmp_path, work_dir):
        """_run_tests passes isolated env to subprocess, preventing coverage file collisions."""
        test_file = work_dir / "test_dummy.py"
        test_file.write_text("def test_ok(): assert True\n")

        captured_env = {}

        original_run = subprocess.run

        def mock_run(cmd, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return original_run(cmd, **{k: v for k, v in kwargs.items() if k != "env"})

        with patch("evaluator.runner.subprocess.run", side_effect=mock_run):
            passed, msg = engine._run_tests(
                work_dir, str(test_file), eval_id="sess-x_impl_mod",
            )

        assert "COVERAGE_FILE" in captured_env
        assert captured_env["COVERAGE_FILE"] == str(work_dir / ".coverage.sess-x_impl_mod")

    def test_coverage_check_uses_isolated_env(self, engine, tmp_path, work_dir):
        """_check_coverage passes isolated env to subprocess."""
        mod_dir = work_dir / "mymod"
        mod_dir.mkdir()
        (mod_dir / "__init__.py").write_text("")
        (mod_dir / "core.py").write_text("def add(a, b): return a + b\n")

        test_file = work_dir / "test_core.py"
        test_file.write_text("from mymod.core import add\ndef test_add(): assert add(1,2)==3\n")

        captured_env = {}

        original_run = subprocess.run

        def mock_run(cmd, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return original_run(cmd, **{k: v for k, v in kwargs.items() if k != "env"})

        with patch("evaluator.runner.subprocess.run", side_effect=mock_run):
            passed, msg, auto = engine._check_coverage(
                work_dir, 50,
                output_artifacts=["mymod/core.py"],
                eval_id="sess-y_eval_core",
            )

        assert "COVERAGE_FILE" in captured_env
        assert captured_env["COVERAGE_FILE"] == str(work_dir / ".coverage.sess-y_eval_core")

    def test_parallel_evaluations_dont_collide(self, engine, tmp_path):
        """Simulate two parallel evaluations — each gets a different COVERAGE_FILE."""
        # Create source and test files so _find_test_files can discover them
        mod_dir = tmp_path / "mymod"
        mod_dir.mkdir()
        (mod_dir / "__init__.py").write_text("")
        (mod_dir / "core.py").write_text("x = 1\n")
        (mod_dir / "utils.py").write_text("y = 2\n")
        test_file = tmp_path / "test_core.py"
        test_file.write_text("from mymod.core import x\ndef test_x(): assert x == 1\n")
        test_file2 = tmp_path / "test_utils.py"
        test_file2.write_text("from mymod.utils import y\ndef test_y(): assert y == 2\n")

        envs = []

        def capture_env(cmd, **kwargs):
            cov = kwargs.get("env", {}).get("COVERAGE_FILE", "")
            if cov:
                envs.append(cov)
            result = MagicMock()
            result.stdout = "TOTAL  100  10  90%"
            result.stderr = ""
            result.returncode = 0
            return result

        with patch("evaluator.runner.subprocess.run", side_effect=capture_env):
            engine.evaluate_stage(
                session_id="sess-par",
                stage_name="impl_a",
                criteria=[SuccessCriterion(type=CriterionType.COVERAGE, target=80, description="coverage")],
                artifact_path=str(tmp_path),
                work_dir=str(tmp_path),
                output_artifacts=["mymod/core.py"],
            )
            engine.evaluate_stage(
                session_id="sess-par",
                stage_name="impl_b",
                criteria=[SuccessCriterion(type=CriterionType.COVERAGE, target=80, description="coverage")],
                artifact_path=str(tmp_path),
                work_dir=str(tmp_path),
                output_artifacts=["mymod/utils.py"],
            )

        assert len(envs) >= 2, f"Expected >= 2 coverage files, got {envs}"
        assert envs[0] != envs[1]
        assert "impl_a" in envs[0]
        assert "impl_b" in envs[1]
