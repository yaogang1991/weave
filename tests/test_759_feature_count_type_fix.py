"""Tests for #759: unify estimate_feature_count type hint and attribute access.

Verifies that:
1. estimate_feature_count accepts None without error
2. estimate_feature_count accepts empty string
3. getattr pattern in node_executor handles missing task_description
"""
from core.node_utils import estimate_feature_count


def test_estimate_feature_count_accepts_none():
    """estimate_feature_count(None) returns 0 (#759)."""
    assert estimate_feature_count(None) == 0


def test_estimate_feature_count_accepts_empty_string():
    """estimate_feature_count('') returns 0 (#759)."""
    assert estimate_feature_count("") == 0


def test_estimate_feature_count_with_complex_task():
    """estimate_feature_count counts features from a complex task."""
    task = (
        "Implement the following: user authentication, "
        "database models, and API endpoints"
    )
    result = estimate_feature_count(task)
    assert result >= 3


def test_getattr_pattern_safe_access():
    """getattr(node, 'task_description', '') works for objects without attribute."""
    from unittest.mock import MagicMock

    # Object without task_description
    obj = MagicMock(spec=[])
    del obj.task_description

    task = getattr(obj, "task_description", "")
    assert estimate_feature_count(task) == 0
