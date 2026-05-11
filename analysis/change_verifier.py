"""
ChangeVerifier — Compare predicted impact scope with actual file changes.

Captures filesystem snapshots before/after execution and computes
coverage metrics: how well the prediction matched reality.
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.models import ImpactScope, VerificationResult

logger = logging.getLogger(__name__)


class ChangeVerifier:
    """Verify that actual changes match predicted impact scope."""

    # File extensions to track for change detection
    _TRACKED_EXTENSIONS = {
        ".py", ".yaml", ".yml", ".json", ".toml", ".cfg",
        ".html", ".css", ".js", ".ts", ".tsx", ".jsx",
    }
    _SKIP_DIRS = {".venv", "venv", "__pycache__", ".git", "node_modules"}

    def __init__(
        self,
        project_path: str,
        coverage_threshold: float = 0.7,
    ) -> None:
        self.project_path = Path(project_path).resolve()
        self.coverage_threshold = coverage_threshold

    def capture_snapshot(self) -> dict[str, tuple[float, int]]:
        """Capture current file modification times and sizes as a baseline snapshot."""
        snapshot: dict[str, tuple[float, int]] = {}
        for ext in self._TRACKED_EXTENSIONS:
            for tracked_file in self.project_path.rglob(f"*{ext}"):
                parts = tracked_file.relative_to(self.project_path).parts
                if any(p in self._SKIP_DIRS for p in parts):
                    continue
                try:
                    rel = str(tracked_file.relative_to(self.project_path))
                    stat = tracked_file.stat()
                    snapshot[rel] = (stat.st_mtime, stat.st_size)
                except OSError:
                    continue
        return snapshot

    def verify(
        self,
        impact_scope: ImpactScope,
        before_snapshot: dict[str, tuple[float, int]],
        after_snapshot: dict[str, tuple[float, int]] | None = None,
    ) -> VerificationResult:
        """Compare actual changes against predicted scope."""
        if after_snapshot is None:
            after_snapshot = self.capture_snapshot()

        actual_changed = self.get_changed_files(before_snapshot, after_snapshot)
        predicted = set(impact_scope.predicted_files)

        covered = sorted(predicted & set(actual_changed))
        unexpected = sorted(set(actual_changed) - predicted)
        missed = sorted(predicted - set(actual_changed))

        # Handle edge case: no actual changes
        if not actual_changed and not predicted:
            coverage = 1.0
            accuracy = 1.0
        elif not actual_changed:
            coverage = 0.0
            accuracy = 0.0
        else:
            coverage = len(covered) / len(actual_changed)
            accuracy = len(covered) / max(len(predicted), 1)

        passes = coverage >= self.coverage_threshold

        notes = ""
        if unexpected:
            notes += f"Unexpected changes: {len(unexpected)} files. "
        if missed:
            notes += f"Missed predictions: {len(missed)} files."

        return VerificationResult(
            impact_scope_id=impact_scope.id,
            expected_files=sorted(predicted),
            actual_changed_files=actual_changed,
            covered_files=covered,
            unexpected_files=unexpected,
            missed_files=missed,
            coverage=coverage,
            prediction_accuracy=accuracy,
            passes=passes,
            notes=notes.strip(),
        )

    def get_changed_files(
        self,
        before: dict[str, tuple[float, int]],
        after: dict[str, tuple[float, int]],
    ) -> list[str]:
        """Diff two snapshots to find changed/new/deleted files."""
        changed: list[str] = []
        all_files = set(before.keys()) | set(after.keys())
        for f in sorted(all_files):
            if f not in before:
                # New file
                changed.append(f)
            elif f not in after:
                # Deleted file
                changed.append(f)
            else:
                before_mtime, before_size = before[f]
                after_mtime, after_size = after[f]
                if abs(before_mtime - after_mtime) > 0.001 or before_size != after_size:
                    # Modified file (mtime or size changed)
                    changed.append(f)
        return changed
