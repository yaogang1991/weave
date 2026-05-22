"""Tests for extract_node_complexity helper (#657)."""
from core.node_utils import extract_node_complexity


class TestExtractNodeComplexity:
    def test_no_artifacts_no_deps(self):
        class Node:
            output_artifacts = []
            dependencies = []

        fc, tc, dc = extract_node_complexity(Node())
        assert fc == 0
        assert tc == 0
        assert dc == 0

    def test_mixed_artifacts(self):
        class Node:
            output_artifacts = ["main.py", "utils.py", "test_foo.py", "test_bar.py"]
            dependencies = ["dep1", "dep2"]

        fc, tc, dc = extract_node_complexity(Node())
        assert fc == 2  # non-test files
        assert tc == 2  # test files
        assert dc == 2  # dependencies

    def test_no_attributes(self):
        """Node without output_artifacts or dependencies returns zeros."""
        fc, tc, dc = extract_node_complexity(object())
        assert fc == 0
        assert tc == 0
        assert dc == 0

    def test_none_artifacts(self):
        class Node:
            output_artifacts = None
            dependencies = ["x"]

        fc, tc, dc = extract_node_complexity(Node())
        assert fc == 0
        assert tc == 0
        assert dc == 1

    def test_all_test_files(self):
        class Node:
            output_artifacts = ["test_a.py", "test_b.py"]
            dependencies = []

        fc, tc, dc = extract_node_complexity(Node())
        assert fc == 0
        assert tc == 2
