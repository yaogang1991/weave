"""Shared node utility helpers.

Extracted from node_executor.py and evaluation_pipeline.py (#657)
to deduplicate stall timeout complexity extraction.
"""
from __future__ import annotations

import re
from typing import Any


def extract_node_complexity(node: Any) -> tuple[int, int, int]:
    """Return (file_count, test_count, dep_count) from node metadata.

    Derives complexity metrics from a DAGNode's output_artifacts and
    dependencies fields. Used by stall timeout scaling calculations.
    """
    file_count = 0
    test_count = 0
    dep_count = 0

    artifacts = getattr(node, "output_artifacts", None) or []
    test_count = sum(
        1 for a in artifacts if "test" in a.lower()
    )
    file_count = len(artifacts) - test_count

    if hasattr(node, "dependencies") and node.dependencies:
        dep_count = len(node.dependencies)

    return file_count, test_count, dep_count


def estimate_feature_count(task_description: str | None) -> int:
    """Estimate distinct features from a task description (#722).

    Uses simple heuristics to count enumerated items and
    comma-separated feature lists. Returns 0 if the task appears
    simple (≤ 3 features).
    """
    if not task_description:
        return 0

    count = 0

    # Pattern 1: Enumerated items "1) X", "2) Y", etc.
    enumerated = re.findall(r'\d+[.)]\s+', task_description)
    if len(enumerated) >= 3:
        count = max(count, len(enumerated))

    # Pattern 2: Comma-separated items after implementation verbs
    feature_list_match = re.findall(
        r'(?:implement|build|create|develop|add)\s+(.+?)(?:\.|$)',
        task_description, re.IGNORECASE,
    )
    for feature_text in feature_list_match:
        items = re.split(r',\s*|\s+and\s+', feature_text)
        items = [i.strip() for i in items if i.strip()]
        count = max(count, len(items))

    # Pattern 3: Verb + noun patterns
    verb_noun = re.findall(
        r'(?:implement|build|create|develop|add|write)\s+\w+',
        task_description, re.IGNORECASE,
    )
    count = max(count, len(verb_noun))

    return count
