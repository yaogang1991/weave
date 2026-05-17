"""CLI impact analysis commands — predict, graph, history (M3.5)."""

from __future__ import annotations

import glob as glob_mod
import json
import os

from core.config import WeaveConfig


async def cmd_impact_predict(args):
    """Predict impact of a change on the project."""
    from analysis.impact_predictor import ImpactPredictor

    config = WeaveConfig.from_env()
    project_path = getattr(args, "project", None) or "."

    predictor = ImpactPredictor(
        memory_manager=None,
        max_predicted_files=config.impact.max_predicted_files,
        confidence_threshold=config.impact.confidence_threshold,
    )
    result = predictor.predict_static(args.requirement, project_path)

    print(json.dumps(result.model_dump(), indent=2, default=str))


async def cmd_impact_graph(args):
    """Show file dependency graph."""
    from analysis.dependency_graph import DependencyGraph

    project_path = getattr(args, "project", None) or "."

    graph = DependencyGraph(project_path)
    graph.build()

    print(json.dumps(graph.to_dict(), indent=2, default=str))


async def cmd_impact_history(args):
    """Show impact analysis history."""
    config = WeaveConfig.from_env()
    impact_path = config.impact.base_path

    if not os.path.isdir(impact_path):
        print(json.dumps({"history": [], "count": 0}))
        return

    records = []
    for f in sorted(
        glob_mod.glob(os.path.join(impact_path, "**", "*.json"), recursive=True),
        reverse=True,
    ):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                records.append(json.load(fh))
        except Exception:
            pass

    print(json.dumps({"history": records[:50], "count": len(records)}, indent=2, default=str))
