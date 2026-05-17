"""
Tests for #240/#246: pre-run check for stdlib-shadowing directories.

Verifies _check_stdlib_shadowing detects and warns about leftover directories
that shadow Python stdlib modules. Non-interactive mode now fail-fast (exit 1)
instead of silently continuing. Legitimate packages are never auto-deleted.
"""
import os
import pytest
from unittest.mock import patch

from main import _check_stdlib_shadowing


class TestCheckStdlibShadowing:
    def test_non_interactive_fail_fast_urllib(self, tmp_path, capsys):
        """Non-interactive: exits with code 1 (fail-fast), does NOT remove."""
        (tmp_path / "urllib").mkdir()
        (tmp_path / "urllib" / "__init__.py").write_text("# shadow", encoding="utf-8")
        with patch.dict(os.environ, {"WEAVE_NON_INTERACTIVE": "true"}):
            with pytest.raises(SystemExit) as exc_info:
                _check_stdlib_shadowing(str(tmp_path))
            assert exc_info.value.code == 1
        # Directory must still exist — never auto-deleted
        assert (tmp_path / "urllib").exists()
        captured = capsys.readouterr()
        assert "shadow" in captured.err.lower()

    def test_non_interactive_fail_fast_json(self, tmp_path, capsys):
        """Non-interactive: exits with code 1, keeps json/ directory."""
        (tmp_path / "json").mkdir()
        with patch.dict(os.environ, {"WEAVE_NON_INTERACTIVE": "true"}):
            with pytest.raises(SystemExit) as exc_info:
                _check_stdlib_shadowing(str(tmp_path))
            assert exc_info.value.code == 1
        assert (tmp_path / "json").exists()

    def test_preserves_non_stdlib_dirs(self, tmp_path):
        """Does not warn about directories that don't shadow stdlib."""
        (tmp_path / "myapp").mkdir()
        (tmp_path / "tests").mkdir()
        with patch.dict(os.environ, {"WEAVE_NON_INTERACTIVE": "true"}):
            _check_stdlib_shadowing(str(tmp_path))
        assert (tmp_path / "myapp").exists()
        assert (tmp_path / "tests").exists()

    def test_skips_dot_dirs(self, tmp_path):
        """Skips hidden directories (e.g., .git, .weave)."""
        (tmp_path / ".git").mkdir()
        (tmp_path / ".weave").mkdir()
        with patch.dict(os.environ, {"WEAVE_NON_INTERACTIVE": "true"}):
            _check_stdlib_shadowing(str(tmp_path))
        assert (tmp_path / ".git").exists()
        assert (tmp_path / ".weave").exists()

    def test_skips_underscore_dirs(self, tmp_path):
        """Skips __pycache__ and similar."""
        (tmp_path / "__pycache__").mkdir()
        with patch.dict(os.environ, {"WEAVE_NON_INTERACTIVE": "true"}):
            _check_stdlib_shadowing(str(tmp_path))
        assert (tmp_path / "__pycache__").exists()

    def test_no_action_when_no_project(self):
        """No-op when project is None."""
        with patch.dict(os.environ, {"WEAVE_NON_INTERACTIVE": "true"}):
            _check_stdlib_shadowing(None)

    def test_no_action_when_project_missing(self):
        """No-op when project path doesn't exist."""
        with patch.dict(os.environ, {"WEAVE_NON_INTERACTIVE": "true"}):
            _check_stdlib_shadowing("/nonexistent/path")

    def test_no_action_when_clean(self, tmp_path):
        """No-op when no shadowing directories exist."""
        (tmp_path / "myproject").mkdir()
        with patch.dict(os.environ, {"WEAVE_NON_INTERACTIVE": "true"}):
            _check_stdlib_shadowing(str(tmp_path))

    def test_non_interactive_fail_fast_multiple(self, tmp_path, capsys):
        """Non-interactive: exits 1, warns about all shadows, keeps them all."""
        (tmp_path / "urllib").mkdir()
        (tmp_path / "json").mkdir()
        (tmp_path / "collections").mkdir()
        with patch.dict(os.environ, {"WEAVE_NON_INTERACTIVE": "true"}):
            with pytest.raises(SystemExit) as exc_info:
                _check_stdlib_shadowing(str(tmp_path))
            assert exc_info.value.code == 1
        assert (tmp_path / "urllib").exists()
        assert (tmp_path / "json").exists()
        assert (tmp_path / "collections").exists()
        captured = capsys.readouterr()
        assert "3 directory" in captured.err

    def test_interactive_keep(self, tmp_path, capsys):
        """Interactive mode: user chooses to keep directories."""
        (tmp_path / "urllib").mkdir()
        with patch.dict(os.environ, {}, clear=True):
            with patch("builtins.input", return_value="n"):
                _check_stdlib_shadowing(str(tmp_path))
        assert (tmp_path / "urllib").exists()

    def test_interactive_quarantine(self, tmp_path, capsys):
        """Interactive mode: user chooses to quarantine leftover dirs."""
        (tmp_path / "urllib").mkdir()
        (tmp_path / "urllib" / "__init__.py").write_text("# x", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            with patch("builtins.input", return_value="y"):
                _check_stdlib_shadowing(str(tmp_path))
        # Original should be gone, quarantine should exist
        assert not (tmp_path / "urllib").exists()
        quarantine_dirs = list((tmp_path / ".weave" / "quarantine").glob("*/*"))
        assert len(quarantine_dirs) == 1
        assert quarantine_dirs[0].name == "urllib"


class TestLegitimatePackageProtection:
    """Legitimate packages matching stdlib names must NOT be deleted or
    quarantined, even in --cleanup-stdlib-shadowing mode."""

    def test_legitimate_json_package_not_deleted_non_interactive(self, tmp_path):
        """Non-interactive with cleanup: legitimate json/ is protected, exits 1."""
        json_dir = tmp_path / "json"
        json_dir.mkdir()
        (json_dir / "__init__.py").write_text(
            "# Legitimate JSON library\nimport typing\n\ndef parse(s: str) -> dict:\n    pass\n",
            encoding="utf-8",
        )
        (json_dir / "encoder.py").write_text("# encoder", encoding="utf-8")
        with patch.dict(os.environ, {"WEAVE_NON_INTERACTIVE": "true"}):
            with pytest.raises(SystemExit) as exc_info:
                _check_stdlib_shadowing(str(tmp_path), cleanup=True)
            assert exc_info.value.code == 1
        # Package must still exist
        assert (json_dir / "__init__.py").exists()
        assert (json_dir / "encoder.py").exists()

    def test_legitimate_email_package_protected_interactive(self, tmp_path, capsys):
        """Interactive: legitimate email/ is protected even when user says yes."""
        email_dir = tmp_path / "email"
        email_dir.mkdir()
        (email_dir / "__init__.py").write_text(
            "# Our custom email module\n"
            "from .sender import send_email\n"
            "from .parser import parse\n",
            encoding="utf-8",
        )
        (email_dir / "sender.py").write_text("def send_email(): pass", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            with patch("builtins.input", return_value="y"):
                _check_stdlib_shadowing(str(tmp_path))
        # Must still exist
        assert (email_dir / "__init__.py").exists()
        assert (email_dir / "sender.py").exists()
        captured = capsys.readouterr()
        assert "PROTECTED" in captured.err

    def test_leftover_empty_dir_quarantined_with_cleanup(self, tmp_path, capsys):
        """Non-interactive with cleanup: empty leftover dir is quarantined."""
        urllib_dir = tmp_path / "urllib"
        urllib_dir.mkdir()
        # No files at all → leftover
        with patch.dict(os.environ, {"WEAVE_NON_INTERACTIVE": "true"}):
            _check_stdlib_shadowing(str(tmp_path), cleanup=True)
        # Original gone, moved to quarantine
        assert not urllib_dir.exists()
        quarantine_dirs = list((tmp_path / ".weave" / "quarantine").glob("*/*"))
        assert len(quarantine_dirs) == 1
        assert quarantine_dirs[0].name == "urllib"

    def test_leftover_trivial_init_quarantined_with_cleanup(self, tmp_path, capsys):
        """Non-interactive with cleanup: trivial __init__.py dir is quarantined."""
        json_dir = tmp_path / "json"
        json_dir.mkdir()
        (json_dir / "__init__.py").write_text("# pass", encoding="utf-8")
        with patch.dict(os.environ, {"WEAVE_NON_INTERACTIVE": "true"}):
            _check_stdlib_shadowing(str(tmp_path), cleanup=True)
        assert not json_dir.exists()
        quarantine_dirs = list((tmp_path / ".weave" / "quarantine").glob("*/*"))
        assert len(quarantine_dirs) == 1

    def test_substantial_init_protected_with_cleanup(self, tmp_path, capsys):
        """Non-interactive with cleanup: substantial __init__.py is protected."""
        json_dir = tmp_path / "json"
        json_dir.mkdir()
        # __init__.py with 50+ chars of real code → legitimate
        (json_dir / "__init__.py").write_text(
            "# JSON utilities for the project\n"
            "import typing\n"
            "from typing import Any, Optional\n"
            "\n"
            "def load(path: str) -> dict[str, Any]:\n"
            "    pass\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"WEAVE_NON_INTERACTIVE": "true"}):
            with pytest.raises(SystemExit) as exc_info:
                _check_stdlib_shadowing(str(tmp_path), cleanup=True)
            assert exc_info.value.code == 1
        assert json_dir.exists()
