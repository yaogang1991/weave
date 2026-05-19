"""MCP tool registrations for Weave analysis capabilities (M4.3).

Exposes project understanding tools (dependency graph, impact prediction,
file snapshots) as MCP tools for external workers (Claude Code, Codex).
All tools are read-only and independently usable without the full Weave
runtime.

Usage::

    from mcp.analysis_tools import register_analysis_tools
    from mcp.server import MCPServer

    server = MCPServer("weave-analysis")
    register_analysis_tools(server)
    asyncio.run(server.run())
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def register_analysis_tools(server) -> None:
    """Register analysis tools on an MCPServer instance."""

    @server.tool(
        "weave.dependency_graph",
        description=(
            "Build and query the file-level dependency graph of a project. "
            "Returns the full graph, or dependencies/dependents for a "
            "specific file when 'file' is specified."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "Project root path (default: current directory)",
                },
                "file": {
                    "type": "string",
                    "description": "Query a specific file's relationships (optional)",
                },
                "direction": {
                    "type": "string",
                    "enum": ["dependents", "dependencies", "all"],
                    "description": "Query direction: dependents (files that depend on this file), "
                    "dependencies (files this file depends on), all (default: all)",
                },
                "depth": {
                    "type": "string",
                    "enum": ["direct", "transitive"],
                    "description": "Traversal depth: direct or transitive (default: transitive)",
                },
            },
        },
    )
    def dependency_graph(
        project: str = ".",
        file: str | None = None,
        direction: str = "all",
        depth: str = "transitive",
    ) -> dict:
        try:
            from analysis.dependency_graph import DependencyGraph

            project_path = Path(project).resolve()
            if not project_path.is_dir():
                return {"error": f"Project path not found: {project}"}

            graph = DependencyGraph(project_path)
            graph.build()

            if file is not None:
                return _query_file_relations(graph, file, direction, depth)

            full_graph = graph.to_dict()
            edge_count = sum(len(deps) for deps in full_graph.values())
            return {
                "project": str(project_path),
                "files": len(full_graph),
                "edges": edge_count,
                "graph": full_graph,
            }
        except Exception as exc:
            logger.error("dependency_graph error: %s", exc, exc_info=True)
            return {"error": str(exc)}

    @server.tool(
        "weave.impact_predict",
        description=(
            "Predict which files a requirement will affect. Uses keyword "
            "matching and dependency graph expansion for static analysis."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "requirement": {
                    "type": "string",
                    "description": "Natural language description of the task/requirement",
                },
                "project": {
                    "type": "string",
                    "description": "Project root path (default: current directory)",
                },
            },
            "required": ["requirement"],
        },
    )
    def impact_predict(requirement: str, project: str = ".") -> dict:
        try:
            from analysis.impact_predictor import ImpactPredictor

            project_path = Path(project).resolve()
            if not project_path.is_dir():
                return {"error": f"Project path not found: {project}"}

            predictor = ImpactPredictor()
            scope = predictor.predict_static(requirement, str(project_path))

            return {
                "requirement": scope.requirement,
                "predicted_files": scope.predicted_files,
                "predicted_modules": scope.predicted_modules,
                "risk_level": scope.risk_level.value,
                "confidence": scope.confidence,
                "reasoning": scope.reasoning,
            }
        except Exception as exc:
            logger.error("impact_predict error: %s", exc, exc_info=True)
            return {"error": str(exc)}

    @server.tool(
        "weave.impact_graph",
        description=(
            "Capture a file snapshot of the project for change tracking. "
            "Returns all tracked source files with their modification times "
            "and sizes. Use before/after snapshots to detect changes."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "Project root path (default: current directory)",
                },
            },
        },
    )
    def impact_graph(project: str = ".") -> dict:
        try:
            from analysis.change_verifier import ChangeVerifier

            project_path = Path(project).resolve()
            if not project_path.is_dir():
                return {"error": f"Project path not found: {project}"}

            verifier = ChangeVerifier(str(project_path))
            snapshot = verifier.capture_snapshot()

            files = [
                {"path": path, "mtime": mtime, "size": size}
                for path, (mtime, size) in sorted(snapshot.items())
            ]

            return {
                "project": str(project_path),
                "tracked_files": len(files),
                "files": files,
            }
        except Exception as exc:
            logger.error("impact_graph error: %s", exc, exc_info=True)
            return {"error": str(exc)}


def _query_file_relations(
    graph, file: str, direction: str, depth: str,
) -> dict:
    """Query a single file's dependency relationships."""
    result: dict = {"file": file}

    if direction in ("dependents", "all"):
        if depth == "direct":
            result["dependents"] = sorted(graph.get_direct_dependents(file))
        else:
            result["dependents"] = sorted(graph.get_dependents(file))

    if direction in ("dependencies", "all"):
        if depth == "direct":
            result["dependencies"] = sorted(graph.get_direct_dependencies(file))
        else:
            result["dependencies"] = sorted(graph.get_dependencies(file))

    return result
