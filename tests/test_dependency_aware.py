"""Tests for orchestrator/dependency_aware.py — dependency-aware decomposition."""
from __future__ import annotations

import pytest
from pathlib import Path

from orchestrator.dependency_aware import DependencyAwareDecomposer, _cluster


@pytest.fixture
def mini_project(tmp_path):
    (tmp_path / "__init__.py").write_text("")
    (tmp_path / "a.py").write_text("from b import func_b\n\ndef func_a():\n    func_b()\n")
    (tmp_path / "b.py").write_text("def func_b():\n    pass\n")
    (tmp_path / "c.py").write_text("import a\n\ndef func_c():\n    a.func_a()\n")
    (tmp_path / "d.py").write_text("# standalone\ndef standalone():\n    pass\n")
    return tmp_path


@pytest.fixture
def decomposer(mini_project):
    return DependencyAwareDecomposer(mini_project)


class TestBuild:
    def test_build_is_idempotent(self, decomposer):
        decomposer.build()
        assert decomposer._built
        decomposer.build()
        assert decomposer._built


class TestGetFileDependencies:
    def test_returns_dependents(self, decomposer):
        decomposer.build()
        deps = decomposer.get_file_dependencies(["b.py"])
        assert isinstance(deps, dict)
        assert "b.py" in deps

    def test_unknown_file_returns_empty(self, decomposer):
        decomposer.build()
        deps = decomposer.get_file_dependencies(["nonexistent.py"])
        assert deps["nonexistent.py"] == []

    def test_empty_list_returns_empty(self, decomposer):
        decomposer.build()
        deps = decomposer.get_file_dependencies([])
        assert deps == {}


class TestSuggestNodeSplit:
    def test_related_files_grouped(self, decomposer):
        decomposer.build()
        clusters = decomposer.suggest_node_split(
            "refactor module", ["a.py", "b.py", "c.py"],
        )
        assert isinstance(clusters, list)
        assert len(clusters) > 0

    def test_standalone_file_own_cluster(self, decomposer):
        decomposer.build()
        clusters = decomposer.suggest_node_split("standalone change", ["d.py"])
        assert len(clusters) == 1
        assert ["d.py"] in clusters


class TestFormatDependencyContext:
    def test_empty_dependencies(self, decomposer):
        result = decomposer.format_dependency_context({})
        assert result == ""

    def test_formats_dependents(self, decomposer):
        deps = {"a.py": ["b.py", "c.py"]}
        result = decomposer.format_dependency_context(deps)
        assert "a.py" in result
        assert "Dependency Analysis" in result

    def test_no_dependents(self, decomposer):
        deps = {"x.py": []}
        result = decomposer.format_dependency_context(deps)
        assert "no project-internal dependents" in result


class TestCluster:
    def test_single_item(self):
        result = _cluster(["a"], {})
        assert result == [["a"]]

    def test_disconnected_items(self):
        result = _cluster(["a", "b", "c"], {})
        assert len(result) == 3

    def test_connected_items_one_cluster(self):
        adj = {"a": {"b"}, "b": {"a"}}
        result = _cluster(["a", "b"], adj)
        assert len(result) == 1
        assert set(result[0]) == {"a", "b"}

    def test_partial_connection(self):
        adj = {"a": {"b"}, "b": {"a"}, "c": set()}
        result = _cluster(["a", "b", "c"], adj)
        assert len(result) == 2

    def test_empty_items(self):
        result = _cluster([], {})
        assert result == []
