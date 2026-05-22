"""Shared node utility helpers.

Extracted from node_executor.py and evaluation_pipeline.py (#657)
to deduplicate stall timeout complexity extraction.
"""
from __future__ import annotations

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
