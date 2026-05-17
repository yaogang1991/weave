"""
ImpactPredictor — Predict which files a requirement will affect.

Uses DependencyGraph for file-level analysis and keyword matching
to predict impact scope. Optionally uses LLM for refinement.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from core.models import ImpactRiskLevel, ImpactScope
from analysis.dependency_graph import DependencyGraph

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[a-z][a-z0-9_]*", re.IGNORECASE)

_STOP_WORDS = {
    "the", "a", "an", "and", "or", "to", "for", "in", "on",
    "of", "with", "is", "are", "be", "been", "was", "were",
    "that", "this", "it", "from", "by", "at", "as", "but",
    "not", "add", "build", "create", "make", "new", "set",
    "get", "do", "can", "will", "should", "would", "could",
    "have", "has", "had", "use", "using", "need", "needs",
    "all", "some", "any", "no", "into", "about", "up",
}


class ImpactPredictor:
    """Predict file/module impact from a natural-language requirement."""

    def __init__(
        self,
        llm_config: Any | None = None,
        memory_manager: Any | None = None,
        max_predicted_files: int = 50,
        confidence_threshold: float = 0.5,
    ) -> None:
        # Reserved for future LLM-based refinement (#463 audit).
        self.llm_config = llm_config
        self.memory_manager = memory_manager
        self.max_predicted_files = max_predicted_files
        self.confidence_threshold = confidence_threshold

    async def predict(
        self,
        requirement: str,
        project_path: str,
    ) -> ImpactScope:
        """Predict the impact scope of a requirement."""
        # Check memory for similar past predictions
        historical = self._get_historical_prediction(requirement, project_path)
        if historical and historical.confidence >= self.confidence_threshold:
            logger.info("Using historical prediction (confidence=%.2f)", historical.confidence)
            return historical

        # Static prediction path
        return self.predict_static(requirement, project_path)

    def predict_static(
        self,
        requirement: str,
        project_path: str,
    ) -> ImpactScope:
        """Static-only prediction using keyword matching + dependency graph."""
        # Build dependency graph
        dep_graph = DependencyGraph(project_path)
        dep_graph.build()

        # Extract keywords for confidence computation
        words = set(_WORD_RE.findall(requirement.lower()))
        keywords = words - _STOP_WORDS

        # Keyword match against file names
        matched_files = self._keyword_match_files(requirement, project_path)

        # Expand with dependency graph
        expanded = self._expand_with_dependencies(matched_files, dep_graph)

        # Deduplicate
        predicted_files = sorted(set(expanded))[:self.max_predicted_files]

        # Extract module names (normalize separators for cross-platform)
        predicted_modules = sorted({
            str(Path(f).parent).replace("\\", "/").replace("/", ".")
            for f in predicted_files
            if "/" in f or "\\" in f
        })

        # Compute risk and confidence
        risk_level = self._compute_risk_level(
            len(predicted_files), len(predicted_modules),
        )
        # Confidence based on match precision: high if keywords map well to files
        keyword_count = len(keywords)
        if keyword_count == 0:
            confidence = 0.0
        elif len(matched_files) == 0:
            confidence = 0.0
        else:
            # More matches per keyword = higher confidence, capped at 1.0
            direct_ratio = min(len(matched_files) / keyword_count, 1.0)
            # Penalize if dependency expansion is large relative to direct matches
            expansion_ratio = len(matched_files) / max(len(predicted_files), 1)
            confidence = direct_ratio * expansion_ratio
            confidence = min(max(confidence, 0.1), 1.0)

        return ImpactScope(
            requirement=requirement,
            predicted_files=predicted_files,
            predicted_modules=predicted_modules,
            risk_level=risk_level,
            confidence=confidence,
            reasoning=(
                f"Static analysis: {len(matched_files)} direct matches, "
                f"{len(predicted_files)} total (with dependencies)"
            ),
        )

    def _keyword_match_files(
        self,
        requirement: str,
        project_path: str,
    ) -> list[str]:
        """Match requirement keywords against file/module names."""
        words = set(_WORD_RE.findall(requirement.lower()))
        # Remove common stop words
        keywords = words - _STOP_WORDS

        matches: list[str] = []
        project = Path(project_path).resolve()
        for py_file in project.rglob("*.py"):
            parts = py_file.relative_to(project).parts
            if any(p in {".venv", "venv", "__pycache__", ".git", "node_modules"} for p in parts):
                continue
            rel = py_file.relative_to(project).as_posix()
            file_stem = py_file.stem.lower()
            rel_lower = rel.lower().replace("/", "_").replace(".py", "")
            for kw in keywords:
                if kw in file_stem or kw in rel_lower:
                    matches.append(rel)
                    break

        return matches

    def _expand_with_dependencies(
        self,
        files: list[str],
        dep_graph: DependencyGraph,
        depth: int = 1,
    ) -> list[str]:
        """Expand initial matches with dependency graph traversal (depth-controlled)."""
        result = list(files)
        current = set(files)
        for _ in range(depth):
            next_level: set[str] = set()
            for f in current:
                # Use direct (non-transitive) dependencies for depth control
                deps = dep_graph.get_direct_dependencies(f)
                deps = {d for d in deps if d not in current}
                next_level.update(deps)
                dependents = dep_graph.get_direct_dependents(f)
                dependents = {d for d in dependents if d not in current}
                next_level.update(dependents)
            result.extend(sorted(next_level))
            current.update(next_level)
        return result

    def _get_historical_prediction(
        self,
        requirement: str,
        project_path: str = "",
    ) -> ImpactScope | None:
        """Check memory for similar past predictions."""
        if self.memory_manager is None:
            return None
        try:
            from memory.manager import _extract_keywords
            keywords = _extract_keywords(requirement, max_keywords=5)
            entries = self.memory_manager.store.search(
                query="impact_analysis " + " ".join(keywords),
                limit=3,
            )
            for entry in entries:
                if "impact_analysis" in entry.keywords:
                    # Skip entries from a different project
                    entry_project = entry.metadata.get("project_path", "")
                    if project_path and entry_project and entry_project != project_path:
                        continue
                    # Reconstruct predicted files from metadata if available
                    predicted_files = entry.metadata.get("predicted_files", [])
                    # Fallback: try to parse file list from content
                    if not predicted_files:
                        import re
                        file_matches = re.findall(
                            r"[\w/]+\.py", entry.content,
                        )
                        predicted_files = file_matches[:10]
                    # Use stored confidence or derive from match quality
                    stored_conf = entry.metadata.get("confidence", 0.6)
                    return ImpactScope(
                        requirement=requirement,
                        predicted_files=predicted_files,
                        confidence=stored_conf,
                        reasoning=f"Based on similar past task: {entry.content[:100]}",
                    )
        except Exception as e:
            logger.debug("Historical prediction lookup failed: %s", e)
        return None

    def _compute_risk_level(
        self,
        file_count: int,
        module_count: int,
    ) -> ImpactRiskLevel:
        """Compute risk from file count and module spread."""
        if file_count == 0:
            return ImpactRiskLevel.LOW
        if file_count <= 2 and module_count <= 1:
            return ImpactRiskLevel.LOW
        if file_count <= 5 and module_count <= 2:
            return ImpactRiskLevel.MEDIUM
        if file_count <= 15 and module_count <= 4:
            return ImpactRiskLevel.HIGH
        return ImpactRiskLevel.CRITICAL
