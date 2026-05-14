"""
Lint issue parsing and delta classification.

Extracted from EvaluatorEngine as part of #178 PR 3.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LintIssue:
    """A single lint issue parsed from flake8 output."""

    path: str  # relative to work_dir
    line: int
    col: int
    code: str  # e.g. "E501", "E402"
    message: str


def parse_flake8_output(output: str) -> list[LintIssue]:
    """Parse flake8 stdout into structured LintIssue list."""
    issues: list[LintIssue] = []
    for line in output.splitlines():
        m = re.match(
            r"^(.+?):(\d+):(\d+):\s+([A-Z]\d+)\s+(.+)$", line,
        )
        if m:
            issues.append(LintIssue(
                path=m.group(1),
                line=int(m.group(2)),
                col=int(m.group(3)),
                code=m.group(4),
                message=m.group(5),
            ))
    return issues


def get_changed_lines(
    file_paths: list[str],
    work_dir: Path,
) -> dict[str, set[int]]:
    """Return {relative_path: set_of_changed_line_numbers} via git diff.

    Uses ``git diff --unified=0`` against HEAD (or index for uncommitted).
    Returns empty dict if git is not available or the directory is not a repo.
    """
    result: dict[str, set[int]] = {}
    for fp in file_paths:
        abs_path = work_dir / fp
        try:
            p = abs_path if abs_path.exists() else Path(fp)
            diff_out = subprocess.run(
                ["git", "diff", "--unified=0", "--", str(p)],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=10,
                cwd=str(work_dir),
            )
            if diff_out.returncode != 0:
                continue
            lines: set[int] = set()
            for hunk in re.finditer(
                r"@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@",
                diff_out.stdout,
            ):
                start = int(hunk.group(1))
                count = int(hunk.group(2) or "1")
                for n in range(start, start + count):
                    lines.add(n)
            if lines:
                try:
                    rel = str(abs_path.relative_to(work_dir))
                except ValueError:
                    rel = fp
                result[rel] = lines
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            continue
    return result
