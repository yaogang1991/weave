"""Tests for #375: artifact_verification uses loose path resolution.

Verifies that _resolve_artifact_path mirrors FileExistsChecker's
stem-based glob fallback, preventing false FAIL when generator
writes to a slightly different path than reported in artifacts.
"""
from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

from evaluator.engine import EvaluatorEngine


class TestResolveArtifactPath:
    """Verify _resolve_artifact_path resolution logic."""

    def test_exact_path_found(self, tmp_path):
        """Exact path match works."""
        (tmp_path / "regex.py").write_text("pass")
        result = EvaluatorEngine._resolve_artifact_path("regex.py", tmp_path)
        assert result is not None
        assert result.name == "regex.py"

    def test_exact_path_with_subdir(self, tmp_path):
        """Exact path with subdirectory works."""
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_foo.py").write_text("pass")
        result = EvaluatorEngine._resolve_artifact_path(
            "tests/test_foo.py", tmp_path,
        )
        assert result is not None
        assert result.name == "test_foo.py"

    def test_stem_glob_fallback_resolves(self, tmp_path):
        """Loose glob finds file in subdirectory via stem match (#375)."""
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_foo.py").write_text("pass")
        # Artifact says "test_foo.py" but file is in "tests/test_foo.py"
        result = EvaluatorEngine._resolve_artifact_path(
            "test_foo.py", tmp_path,
        )
        assert result is not None
        assert "test_foo" in result.name

    def test_stem_glob_prefers_exact_match(self, tmp_path):
        """When exact match exists, it's preferred over glob."""
        (tmp_path / "app.py").write_text("exact")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("glob")
        result = EvaluatorEngine._resolve_artifact_path("app.py", tmp_path)
        assert result is not None
        assert result.read_text() == "exact"

    def test_stem_glob_short_name_skipped(self, tmp_path):
        """Short stems (< 3 chars) don't trigger glob fallback."""
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "ab.py").write_text("pass")
        result = EvaluatorEngine._resolve_artifact_path("ab.py", tmp_path)
        # stem "ab" has length 2 < 3, so glob skipped
        assert result is None

    def test_missing_file_returns_none(self, tmp_path):
        """Truly missing file returns None."""
        result = EvaluatorEngine._resolve_artifact_path(
            "nonexistent.py", tmp_path,
        )
        assert result is None

    def test_absolute_path_resolved(self, tmp_path):
        """Absolute paths are resolved correctly."""
        f = tmp_path / "real.py"
        f.write_text("pass")
        result = EvaluatorEngine._resolve_artifact_path(str(f), tmp_path)
        assert result is not None
        assert result == f
