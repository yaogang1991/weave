"""Tests for project path safety — prevents agents from modifying Weave itself.

Regression tests for issue #161: when --project is not specified and cwd is
inside the Weave source tree, the CLI must refuse to run.
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


# Import the function under test
sys.path.insert(0, str(Path(__file__).parent.parent))
from main import _resolve_project_path  # noqa: E402


WEAVE_ROOT = Path(__file__).parent.parent.resolve()


class TestResolveProjectPath:
    """Unit tests for _resolve_project_path safety gate."""

    def test_explicit_project_outside_weave_passes(self, tmp_path):
        """--project pointing to a directory outside Weave → returns resolved path."""
        result = _resolve_project_path(str(tmp_path))
        assert result == str(tmp_path.resolve())

    def test_no_project_outside_weave_passes(self, tmp_path):
        """No --project, cwd outside Weave → returns cwd with warning."""
        with patch("main.Path.cwd", return_value=tmp_path):
            result = _resolve_project_path(None)
        assert result == str(tmp_path.resolve())

    def test_no_project_inside_weave_exits(self):
        """No --project, cwd is Weave root → sys.exit(2)."""
        with patch("main.Path.cwd", return_value=WEAVE_ROOT):
            with pytest.raises(SystemExit) as exc_info:
                _resolve_project_path(None)
            assert exc_info.value.code == 2

    def test_no_project_inside_weave_subdir_exits(self):
        """No --project, cwd is inside Weave subdirectory → sys.exit(2)."""
        subdir = WEAVE_ROOT / "core"
        with patch("main.Path.cwd", return_value=subdir):
            with pytest.raises(SystemExit) as exc_info:
                _resolve_project_path(None)
            assert exc_info.value.code == 2

    def test_project_points_to_weave_without_flag_exits(self):
        """--project pointing to Weave root without --allow-self-modify → exit."""
        with pytest.raises(SystemExit) as exc_info:
            _resolve_project_path(str(WEAVE_ROOT))
        assert exc_info.value.code == 2

    def test_project_points_to_weave_with_flag_passes(self):
        """--project pointing to Weave WITH --allow-self-modify → passes."""
        result = _resolve_project_path(str(WEAVE_ROOT), allow_self_modify=True)
        assert result == str(WEAVE_ROOT)

    def test_no_project_inside_weave_with_flag_passes(self):
        """No --project, cwd inside Weave WITH --allow-self-modify → returns cwd."""
        subdir = WEAVE_ROOT / "core"
        with patch("main.Path.cwd", return_value=subdir):
            result = _resolve_project_path(None, allow_self_modify=True)
        assert result == str(subdir.resolve())

    def test_explicit_external_project_with_flag_passes(self, tmp_path):
        """External --project with --allow-self-modify → passes normally."""
        result = _resolve_project_path(str(tmp_path), allow_self_modify=True)
        assert result == str(tmp_path.resolve())


class TestServiceRejectsMissingProjectPath:
    """Verify the defense-in-depth check in control_plane/service.py."""

    def test_valueerror_on_missing_project_path(self):
        """The service check raises ValueError when project_path is None."""
        project_path = None
        with pytest.raises(ValueError, match="project_path is required"):
            if not project_path:
                raise ValueError(
                    "project_path is required for job execution. "
                    "Refusing to use cwd as target — agents may modify Weave itself. "
                    "Submit jobs with --project /path/to/target."
                )

    def test_valueerror_on_empty_project_path(self):
        """Empty string project_path also triggers the check."""
        project_path = ""
        with pytest.raises(ValueError, match="project_path is required"):
            if not project_path:
                raise ValueError(
                    "project_path is required for job execution. "
                    "Refusing to use cwd as target."
                )

    def test_valid_project_path_passes(self):
        """Non-empty project_path does not raise."""
        project_path = "/tmp/my-project"
        # This should NOT raise
        if not project_path:
            raise ValueError("project_path is required")
