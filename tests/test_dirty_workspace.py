"""Tests for #147 Phase 1: dirty workspace warning before re-runs.

Covers:
- Clean workspace: no warning, proceeds normally
- Dirty workspace: warning printed to stderr
- Non-interactive mode: warning but no prompt
- Interactive mode with 'n': aborts
- Non-git directory: no check
- No project: skip check
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from main import _check_dirty_workspace  # noqa: E402


class TestCheckDirtyWorkspace:
    """_check_dirty_workspace warns about uncommitted changes."""

    def test_clean_workspace_no_warning(self, tmp_path, capsys):
        """Clean git repo → no warning."""
        # Init a clean git repo
        import subprocess
        subprocess.run(
            ["git", "init"], cwd=str(tmp_path),
            capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(tmp_path), capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(tmp_path), capture_output=True, timeout=10,
        )

        _check_dirty_workspace(str(tmp_path))
        captured = capsys.readouterr()
        assert "WARN" not in captured.err

    def test_dirty_workspace_warns(self, tmp_path, capsys):
        """Dirty git repo → warning printed."""
        import subprocess
        subprocess.run(
            ["git", "init"], cwd=str(tmp_path),
            capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(tmp_path), capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(tmp_path), capture_output=True, timeout=10,
        )
        (tmp_path / "dirty.py").write_text("x = 1")

        # Non-interactive to avoid stdin prompt
        with patch.dict(os.environ, {"HARNESS_NON_INTERACTIVE": "true"}):
            _check_dirty_workspace(str(tmp_path))

        captured = capsys.readouterr()
        assert "uncommitted" in captured.err.lower()

    def test_non_interactive_proceeds(self, tmp_path, capsys):
        """Non-interactive mode → warns but proceeds."""
        import subprocess
        subprocess.run(
            ["git", "init"], cwd=str(tmp_path),
            capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(tmp_path), capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(tmp_path), capture_output=True, timeout=10,
        )
        (tmp_path / "dirty.py").write_text("x = 1")

        with patch.dict(os.environ, {"HARNESS_NON_INTERACTIVE": "true"}):
            _check_dirty_workspace(str(tmp_path))  # Should NOT raise

        captured = capsys.readouterr()
        assert "non-interactive" in captured.err.lower()

    def test_interactive_abort(self, tmp_path):
        """Interactive mode + user says no → SystemExit."""
        import subprocess
        subprocess.run(
            ["git", "init"], cwd=str(tmp_path),
            capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(tmp_path), capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(tmp_path), capture_output=True, timeout=10,
        )
        (tmp_path / "dirty.py").write_text("x = 1")

        with patch.dict(os.environ, {"HARNESS_NON_INTERACTIVE": ""}):
            with patch("builtins.input", return_value="n"):
                with pytest.raises(SystemExit) as exc:
                    _check_dirty_workspace(str(tmp_path))
                assert exc.value.code == 1

    def test_interactive_continue(self, tmp_path, capsys):
        """Interactive mode + user says yes → proceeds."""
        import subprocess
        subprocess.run(
            ["git", "init"], cwd=str(tmp_path),
            capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(tmp_path), capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(tmp_path), capture_output=True, timeout=10,
        )
        (tmp_path / "dirty.py").write_text("x = 1")

        with patch.dict(os.environ, {"HARNESS_NON_INTERACTIVE": ""}):
            with patch("builtins.input", return_value="y"):
                _check_dirty_workspace(str(tmp_path))  # Should NOT raise

    def test_non_git_dir_no_check(self, tmp_path, capsys):
        """Non-git directory → no check, no warning."""
        _check_dirty_workspace(str(tmp_path))
        captured = capsys.readouterr()
        assert "uncommitted" not in captured.err.lower()

    def test_no_project_skips(self, capsys):
        """project=None → skip entirely."""
        _check_dirty_workspace(None)
        captured = capsys.readouterr()
        assert captured.err == ""
