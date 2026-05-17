"""
Tests for #283: autoflake --in-place auto-fix before lint scoring.

Verifies that unused imports (F401) and unused variables (F841) are
automatically removed before flake8 scoring, preventing the retry loop
where the generator regenerates files with the same unused imports.
"""
import pytest
from unittest.mock import MagicMock, patch

from evaluator.engine import EvaluatorEngine
from session.store import SessionStore


@pytest.fixture
def tmp_store(tmp_path):
    return SessionStore(str(tmp_path / "events"))


@pytest.fixture
def engine(tmp_store):
    return EvaluatorEngine(tmp_store)


class TestAutoFixUnused:
    def test_removes_unused_imports_in_place(self, engine, tmp_path):
        """autoflake --in-place actually removes unused imports from files."""
        code_file = tmp_path / "mod.py"
        code_file.write_text("import os\nimport json\n\nx = 1\n", encoding="utf-8")

        with patch("evaluator.runner.subprocess.run") as mock_run:
            # First call: autoflake in-place (modifies the file)
            # Second call: flake8 (returns clean)
            mock_run.return_value = MagicMock(returncode=0, stdout="")

            passed, msg = engine._run_lint(["mod.py"], tmp_path)

        assert passed
        # The autoflake in-place call should have been made
        autoflake_call = mock_run.call_args_list[0]
        assert "--in-place" in autoflake_call[0][0]

    def test_tracks_autofixed_files(self, engine, tmp_path):
        """_auto_fix_unused returns relative paths of modified files."""
        code_file = tmp_path / "mod.py"
        code_file.write_text("import os\nimport json\n\nx = 1\n", encoding="utf-8")

        # Simulate autoflake actually modifying the file
        def fake_run(cmd, **kwargs):
            if "autoflake" in cmd:
                # Simulate in-place fix: remove unused imports
                code_file.write_text("import os\n\nx = 1\n", encoding="utf-8")
            return MagicMock(returncode=0, stdout="")

        with patch("evaluator.runner.subprocess.run", side_effect=fake_run):
            changed = engine._auto_fix_unused([str(code_file)], tmp_path)

        assert len(changed) == 1
        assert "mod.py" in changed[0]

    def test_no_changes_when_clean(self, engine, tmp_path):
        """_auto_fix_unused returns empty list when no changes needed."""
        code_file = tmp_path / "clean.py"
        code_file.write_text("import os\n\nos.path.exists('.')\n", encoding="utf-8")

        with patch("evaluator.runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            changed = engine._auto_fix_unused([str(code_file)], tmp_path)

        assert changed == []

    def test_autoflake_not_installed_graceful(self, engine, tmp_path):
        """Gracefully handles autoflake not being installed."""
        code_file = tmp_path / "mod.py"
        code_file.write_text("import os\n\nx = 1\n", encoding="utf-8")

        with patch("evaluator.runner.subprocess.run", side_effect=FileNotFoundError):
            changed = engine._auto_fix_unused([str(code_file)], tmp_path)

        assert changed == []

    def test_autoflake_timeout_graceful(self, engine, tmp_path):
        """Gracefully handles autoflake timing out."""
        import subprocess
        code_file = tmp_path / "mod.py"
        code_file.write_text("import os\n\nx = 1\n", encoding="utf-8")

        with patch("evaluator.runner.subprocess.run",
                    side_effect=subprocess.TimeoutExpired("autoflake", 30)):
            changed = engine._auto_fix_unused([str(code_file)], tmp_path)

        assert changed == []

    def test_last_autofixed_populated(self, engine, tmp_path):
        """_last_autofixed is populated with files fixed by _auto_fix_unused."""
        code_file = tmp_path / "mod.py"
        code_file.write_text("import os\nimport json\n\nx = 1\n", encoding="utf-8")

        def fake_run(cmd, **kwargs):
            if "autoflake" in cmd:
                code_file.write_text("import os\n\nx = 1\n", encoding="utf-8")
            return MagicMock(returncode=0, stdout="")

        with patch("evaluator.runner.subprocess.run", side_effect=fake_run):
            engine._run_lint(["mod.py"], tmp_path)

        assert len(engine._last_autofixed) == 1
        assert "mod.py" in engine._last_autofixed[0]

    def test_empty_targets_returns_empty(self, engine, tmp_path):
        """Returns empty list when no targets provided."""
        changed = engine._auto_fix_unused([], tmp_path)
        assert changed == []

    def test_multiple_files_tracked(self, engine, tmp_path):
        """Tracks multiple files when autoflake modifies them."""
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("import os\n\nx = 1\n", encoding="utf-8")
        f2.write_text("import json\n\ny = 2\n", encoding="utf-8")

        def fake_run(cmd, **kwargs):
            if "autoflake" in cmd:
                f1.write_text("import os\n\nx = 1\n", encoding="utf-8")  # unchanged
                f2.write_text("y = 2\n", encoding="utf-8")  # changed
            return MagicMock(returncode=0, stdout="")

        with patch("evaluator.runner.subprocess.run", side_effect=fake_run):
            changed = engine._auto_fix_unused([str(f1), str(f2)], tmp_path)

        assert len(changed) == 1
        assert "b.py" in changed[0]
