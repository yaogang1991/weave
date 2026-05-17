"""Artifact path resolution and scope filtering.

Extracted from EvaluatorEngine for maintainability (#440).
These functions are stateless — all context is passed as arguments.
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.models import CriterionType, SuccessCriterion

logger = logging.getLogger(__name__)


def resolve_artifact_path(
    artifact: str,
    eval_root: Path,
) -> Path | None:
    """Resolve an artifact path on disk, using loose fallback.

    Mirrors the resolution logic in FileExistsChecker._check_file_exists
    to avoid false mismatches between FILE_EXISTS criteria (which use
    stem-based glob fallback) and artifact_verification (which previously
    only checked exact paths).

    Returns the resolved absolute Path, or None if not found.
    """
    p = Path(artifact)
    full = p if p.is_absolute() else eval_root / p

    # Exact match
    if full.is_file():
        return full

    # Fallback: loose glob by stem (mirrors FileExistsChecker)
    stem = p.stem
    if stem and len(stem) >= 3:
        matches = list(eval_root.glob(f"**/*{stem}*"))
        matches = [m for m in matches if m.is_file()]
        if matches:
            return matches[0]

    return None


def scope_artifacts_to_criteria(
    output_artifacts: list[str] | None,
    criteria: list[SuccessCriterion],
    work_dir: Path | None,
    owned_files: list[str] | None = None,
) -> list[str] | None:
    """Filter output_artifacts to only files the node is supposed to own.

    When success_criteria include file_exists or file_pattern entries,
    only return artifacts matching those specifications. This prevents
    cross-node lint contamination when parallel generator nodes share
    the same workspace (#320).

    When owned_files is provided, artifacts are further filtered to only
    include files the node is responsible for, preventing cross-node lint
    pollution (#395).

    If no file-based criteria exist and owned_files is not provided,
    return artifacts unchanged.
    """
    if not output_artifacts:
        return output_artifacts

    # Filter by owned_files first (#395)
    if owned_files:
        owned_set = set(owned_files)
        filtered = []
        for art in output_artifacts:
            art_rel = art
            if work_dir:
                try:
                    p = Path(art)
                    if p.is_absolute():
                        art_rel = str(p.relative_to(work_dir))
                except ValueError:
                    pass
            if any(
                art_rel == own
                or art_rel.endswith("/" + own)
                or own.endswith("/" + art_rel)
                for own in owned_set
            ):
                filtered.append(art)
        if filtered:
            if len(filtered) < len(output_artifacts):
                logger.info(
                    "Scoped artifacts from %d to %d based on owned_files (#395)",
                    len(output_artifacts), len(filtered),
                )
            output_artifacts = filtered
        else:
            # owned_files didn't match any artifact — keep originals
            # to avoid false negatives when owned_files is stale
            pass

    # Collect expected file paths/patterns from criteria
    expected_paths: list[str] = []
    expected_patterns: list[str] = []
    for crit in criteria:
        if crit.type == CriterionType.FILE_EXISTS and crit.path:
            expected_paths.append(crit.path)
        elif crit.type == CriterionType.FILE_PATTERN and crit.pattern:
            expected_patterns.append(crit.pattern)

    # No file-based criteria -> no scoping possible, return as-is
    if not expected_paths and not expected_patterns:
        return output_artifacts

    # Expand patterns to concrete file matches
    expected_files: set[str] = set(expected_paths)
    if expected_patterns and work_dir:
        for pattern in expected_patterns:
            for match in work_dir.glob(pattern):
                if match.is_file():
                    try:
                        expected_files.add(str(match.relative_to(work_dir)))
                    except ValueError:
                        expected_files.add(str(match))

    # Filter artifacts to only expected files
    scoped = []
    for art in output_artifacts:
        # Normalize: try relative path
        art_rel = art
        if work_dir:
            try:
                p = Path(art)
                if p.is_absolute():
                    art_rel = str(p.relative_to(work_dir))
            except ValueError:
                pass
        # Match against expected files (prefix match for directories)
        for expected in expected_files:
            if (
                art_rel == expected
                or art_rel.endswith("/" + expected)
                or expected.endswith("/" + art_rel)
            ):
                scoped.append(art)
                break
        else:
            # Also check if artifact is directly in expected_paths
            if art in expected_paths or any(art.endswith("/" + e) for e in expected_paths):
                scoped.append(art)

    if scoped:
        if len(scoped) < len(output_artifacts):
            logger.info(
                "Scoped artifacts from %d to %d based on criteria (#320)",
                len(output_artifacts), len(scoped),
            )
        return scoped

    # If scoping removed everything, fall back to original artifacts
    return output_artifacts
