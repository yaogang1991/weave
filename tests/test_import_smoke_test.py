"""Tests for #344: import smoke test in evaluator engine.

Verifies that the evaluator catches ImportErrors, NameErrors, and
SyntaxErrors in generated source files that flake8 cannot detect
(e.g., hallucinated stdlib function arguments).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from session.store import SessionStore


def _make_engine():
    """Create an EvaluatorEngine with mocked session store."""
    from evaluator.engine import EvaluatorEngine

    store = MagicMock(spec=SessionStore)
    return EvaluatorEngine(session_store=store)


def test_import_smoke_test_catches_import_error(tmp_path):
    """A source file with a bad import should fail the smoke test."""
    engine = _make_engine()

    # Create a file with a non-existent import
    src = tmp_path / "mymod.py"
    src.write_text("from nonexistent_module import foo")

    result = engine.evaluate_stage(
        session_id="test",
        stage_name="impl",
        criteria=["tests_pass"],
        artifact_path=str(tmp_path),
        work_dir=str(tmp_path),
        output_artifacts=["mymod.py"],
    )
    # Should have import_check feedback
    assert "import_check" in result.feedback
    assert "mymod.py" in result.feedback


def test_import_smoke_test_passes_valid_module(tmp_path):
    """A source file with valid imports should pass the smoke test."""
    engine = _make_engine()

    src = tmp_path / "goodmod.py"
    src.write_text("import os\n\nPATH = os.getcwd()")

    result = engine.evaluate_stage(
        session_id="test",
        stage_name="impl",
        criteria=["lint"],
        artifact_path=str(tmp_path),
        work_dir=str(tmp_path),
        output_artifacts=["goodmod.py"],
    )
    # Should NOT have import_check in feedback (no errors)
    assert "import_check" not in result.feedback


def test_import_smoke_test_skips_test_files(tmp_path):
    """Test files should be skipped by the import smoke test."""
    engine = _make_engine()

    # A test file with a bad import — should NOT fail
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    test_file = tests_dir / "test_bad.py"
    test_file.write_text("from nonexistent_module import foo")

    result = engine.evaluate_stage(
        session_id="test",
        stage_name="impl",
        criteria=["lint"],
        artifact_path=str(tmp_path),
        work_dir=str(tmp_path),
        output_artifacts=["tests/test_bad.py"],
    )
    # Test files should be skipped
    assert "import_check" not in result.feedback


def test_import_smoke_test_skips_non_py_files(tmp_path):
    """Non-Python files should be skipped."""
    engine = _make_engine()

    data_file = tmp_path / "data.json"
    data_file.write_text('{"key": "value"}')

    errors = engine._import_smoke_test(
        ["data.json"], tmp_path,
    )
    assert errors == []


def test_import_smoke_test_skips_missing_files(tmp_path):
    """Non-existent files should be skipped gracefully."""
    engine = _make_engine()

    errors = engine._import_smoke_test(
        ["nonexistent.py"], tmp_path,
    )
    assert errors == []


def test_import_smoke_test_subdirectory(tmp_path):
    """Source files in subdirectories should use dot-path imports."""
    engine = _make_engine()

    subdir = tmp_path / "mylib"
    subdir.mkdir()
    src = subdir / "core.py"
    src.write_text("import os\n\nval = os.path.exists('.')")

    errors = engine._import_smoke_test(
        ["mylib/core.py"], tmp_path,
    )
    assert errors == []


def test_import_smoke_test_timeout(tmp_path):
    """Import that hangs should be caught by timeout."""
    engine = _make_engine()

    # Create a file that blocks on import
    src = tmp_path / "hangmod.py"
    src.write_text(
        "import time\n"
        "time.sleep(300)  # Block forever\n"
    )

    errors = engine._import_smoke_test(
        ["hangmod.py"], tmp_path,
    )
    assert len(errors) == 1
    assert "timed out" in errors[0][1].lower()


def test_import_smoke_test_no_artifacts(tmp_path):
    """When no artifacts, smoke test should return empty."""
    engine = _make_engine()

    errors = engine._import_smoke_test([], tmp_path)
    assert errors == []
