"""
DependencyAwareDecomposer — Dependency-aware task decomposition for planning.

Wraps analysis/dependency_graph.DependencyGraph to provide file-level
dependency analysis and cluster-based node split suggestions that can be
injected into the LLM planning prompt.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path

from analysis.dependency_graph import DependencyGraph

logger = logging.getLogger(__name__)


class DependencyAwareDecomposer:
    """Build dependency context from a project and format it for the planner."""

    def __init__(self, project_path: str | Path) -> None:
        self.project_path = Path(project_path).resolve()
        self._graph = DependencyGraph(self.project_path)
        self._built = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(self) -> None:
        """Scan the project and build the dependency graph (idempotent)."""
        if self._built:
            return
        self._graph.build()
        self._built = True
        logger.info(
            "DependencyAwareDecomposer built graph for %s",
            self.project_path,
        )

    def get_file_dependencies(
        self, file_paths: list[str],
    ) -> dict[str, list[str]]:
        """Return ``{file: [files that depend on it]}`` for each path."""
        self.build()
        result: dict[str, list[str]] = {}
        for fp in file_paths:
            dependents = self._graph.get_dependents(fp)
            result[fp] = sorted(dependents)
        return result

    def suggest_node_split(
        self, requirement: str, affected_files: list[str],
    ) -> list[list[str]]:
        """Group *affected_files* into dependency clusters.

        Files that share direct dependency or dependent relationships are
        placed in the same cluster so the planner keeps them in one node.
        """
        self.build()

        # Build adjacency for clustering (bidirectional within affected set)
        affected_set = set(affected_files)
        adjacency: dict[str, set[str]] = defaultdict(set)
        for fp in affected_files:
            for dep in self._graph.get_direct_dependencies(fp):
                if dep in affected_set:
                    adjacency[fp].add(dep)
                    adjacency[dep].add(fp)
            for dep in self._graph.get_direct_dependents(fp):
                if dep in affected_set:
                    adjacency[fp].add(dep)
                    adjacency[dep].add(fp)

        # Union-Find clustering
        clusters = _cluster(affected_files, adjacency)
        logger.info(
            "suggest_node_split: %d files -> %d clusters",
            len(affected_files), len(clusters),
        )
        return clusters

    def format_dependency_context(
        self, dependencies: dict[str, list[str]],
    ) -> str:
        """Format dependency info for injection into the planning prompt."""
        if not dependencies:
            return ""

        lines = [
            "## Dependency Analysis",
            "The following files share dependencies and should be modified "
            "in the same node when changes are related:",
            "",
        ]
        for file_path, dependents in sorted(dependencies.items()):
            if dependents:
                deps = ", ".join(f"`{d}`" for d in dependents)
                lines.append(f"- `{file_path}` is imported by: {deps}")
            else:
                lines.append(f"- `{file_path}` has no project-internal dependents")

        # Grouping hint
        clusters = self._infer_clusters_from_deps(dependencies)
        if clusters:
            lines.append("")
            lines.append("Suggested grouping (files that should be in the same node):")
            for i, cluster in enumerate(clusters, 1):
                lines.append(f"  Group {i}: {', '.join(f'`{f}`' for f in cluster)}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _infer_clusters_from_deps(
        self, dependencies: dict[str, list[str]],
    ) -> list[list[str]]:
        """Infer clusters from a dependency map using union-find."""
        all_files = set(dependencies.keys())
        for deps in dependencies.values():
            all_files.update(deps)

        adjacency: dict[str, set[str]] = defaultdict(set)
        for fp, deps in dependencies.items():
            for dep in deps:
                adjacency[fp].add(dep)
                adjacency[dep].add(fp)

        return _cluster(sorted(all_files), adjacency)


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _cluster(
    items: list[str], adjacency: dict[str, set[str]],
) -> list[list[str]]:
    """Union-Find clustering over *items* using *adjacency* edges."""
    parent: dict[str, str] = {item: item for item in items}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for item in items:
        for neighbor in adjacency.get(item, set()):
            if neighbor in parent:
                union(item, neighbor)

    groups: dict[str, list[str]] = defaultdict(list)
    for item in items:
        groups[find(item)].append(item)

    # Return clusters with >1 file first (most useful for grouping)
    result = [sorted(g) for g in groups.values()]
    result.sort(key=lambda g: (-len(g), g))
    return result
