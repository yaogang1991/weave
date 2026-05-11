"""
DependencyGraph — Build and query file-level dependency graphs.

Uses Python's ast module to statically analyze import statements and
build a bidirectional file dependency graph. Pure stdlib, no LLM needed.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SKIP_DIRS = {
    ".venv", "venv", "node_modules", "__pycache__",
    ".git", ".tox", "dist", "build", ".eggs", ".mypy_cache",
}


class DependencyGraph:
    """Build and query file-level dependency graphs from project structure."""

    def __init__(self, project_path: str | Path) -> None:
        self.project_path = Path(project_path).resolve()
        self._graph: dict[str, set[str]] = {}      # file -> files it depends on
        self._reverse: dict[str, set[str]] = {}     # file -> files that depend on it
        self._module_map: dict[str, str] = {}       # module_name -> relative file path

    def build(self) -> dict[str, set[str]]:
        """Scan project and build import dependency graph."""
        self._graph.clear()
        self._reverse.clear()
        self._module_map.clear()

        py_files = self._find_python_files()
        for rel_path in py_files:
            self._graph.setdefault(rel_path, set())
            self._reverse.setdefault(rel_path, set())

        # Build module -> file mapping
        for rel_path in py_files:
            module_name = self._path_to_module(rel_path)
            self._module_map[module_name] = rel_path

        # Parse imports
        for rel_path in py_files:
            abs_path = self.project_path / rel_path
            try:
                imports = self._parse_imports(abs_path)
                for imp_module in imports:
                    dep_path = self._resolve_module(imp_module, rel_path)
                    if dep_path and dep_path in self._graph:
                        self._graph[rel_path].add(dep_path)
                        self._reverse.setdefault(dep_path, set()).add(rel_path)
            except Exception:
                logger.debug("Skipping %s: parse error", rel_path)

        logger.info(
            "Built dependency graph: %d files, %d edges",
            len(self._graph),
            sum(len(deps) for deps in self._graph.values()),
        )
        return self._graph

    def get_dependents(self, file_path: str) -> set[str]:
        """Get all files that depend on the given file (transitive)."""
        visited: set[str] = set()
        self._traverse(file_path, self._reverse, visited)
        visited.discard(file_path)
        return visited

    def get_dependencies(self, file_path: str) -> set[str]:
        """Get all files the given file depends on (transitive)."""
        visited: set[str] = set()
        self._traverse(file_path, self._graph, visited)
        visited.discard(file_path)
        return visited

    def get_direct_dependents(self, file_path: str) -> set[str]:
        """Get files that directly depend on the given file (non-transitive)."""
        return set(self._reverse.get(file_path, set()))

    def get_direct_dependencies(self, file_path: str) -> set[str]:
        """Get files the given file directly depends on (non-transitive)."""
        return set(self._graph.get(file_path, set()))

    def get_module_files(self, module_name: str) -> list[str]:
        """Map a module name to file paths in the project."""
        results: list[str] = []
        for mod, path in self._module_map.items():
            if mod.startswith(module_name):
                results.append(path)
        return results

    def to_dict(self) -> dict[str, list[str]]:
        """Serialize graph for storage/display."""
        return {
            k: sorted(v) for k, v in self._graph.items()
        }

    def _traverse(
        self,
        start: str,
        graph: dict[str, set[str]],
        visited: set[str],
    ) -> None:
        if start not in graph or start in visited:
            return
        visited.add(start)
        for neighbor in graph[start]:
            self._traverse(neighbor, graph, visited)

    def _find_python_files(self) -> list[str]:
        """Find all Python files relative to project root."""
        results: list[str] = []
        for path in self.project_path.rglob("*.py"):
            parts = path.relative_to(self.project_path).parts
            if any(part in _SKIP_DIRS for part in parts):
                continue
            results.append(str(path.relative_to(self.project_path)))
        return sorted(results)

    def _path_to_module(self, rel_path: str) -> str:
        """Convert a relative file path to a Python module name."""
        p = Path(rel_path)
        if p.name == "__init__.py":
            parts = p.parent.parts
        else:
            parts = p.with_suffix("").parts
        return ".".join(parts)

    def _parse_imports(self, abs_path: Path) -> list[str]:
        """Extract import module names from a Python file."""
        source = abs_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    # Absolute import
                    if node.module:
                        imports.append(node.module)
                        # Also resolve imported names as submodules:
                        # from app import models -> app.models
                        for alias in node.names:
                            imports.append(f"{node.module}.{alias.name}")
                elif node.level > 0:
                    # Relative import: from . import X, from ..foo import bar
                    mods = self._resolve_relative_import(node, abs_path)
                    imports.extend(mods)
        return imports

    def _resolve_relative_import(
        self, node: ast.ImportFrom, abs_path: Path,
    ) -> list[str]:
        """Resolve a relative import to absolute module names.

        Handles: from . import X, from .foo import bar, from .. import X, etc.
        Returns a list to support multi-name imports like `from . import a, b`.
        """
        rel_path = str(abs_path.relative_to(self.project_path))
        parts = Path(rel_path).parts

        # Determine package: directory containing the file
        package_parts = list(parts[:-1])

        # Go up (level - 1) levels from the package
        level = node.level
        if level - 1 > len(package_parts):
            return []
        if level > 1:
            base_parts = package_parts[:len(package_parts) - (level - 1)]
        else:
            base_parts = package_parts

        if node.module:
            mod = ".".join(base_parts + [node.module]) if base_parts else node.module
            results = [mod]
            # Also resolve imported names as submodules:
            # from .pkg import mod -> app.pkg + app.pkg.mod
            for alias in node.names:
                results.append(f"{mod}.{alias.name}")
            return results
        else:
            # from . import X — resolve each imported name as a sibling module
            results: list[str] = []
            for alias in node.names:
                if base_parts:
                    results.append(".".join(base_parts + [alias.name]))
                else:
                    results.append(alias.name)
            return results

    def _resolve_module(self, module_name: str, from_file: str) -> str | None:
        """Resolve an import module name to a project-relative file path."""
        if module_name in self._module_map:
            return self._module_map[module_name]
        # Try prefix match (e.g., "core.models" -> "core/models.py")
        parts = module_name.split(".")
        candidate = str(Path(*parts)) + ".py"
        if candidate in self._graph:
            return candidate
        # Try as package
        candidate_init = str(Path(*parts) / "__init__.py")
        if candidate_init in self._graph:
            return candidate_init
        return None
