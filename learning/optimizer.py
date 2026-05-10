"""
LearningOptimizer — Convert learning insights into actionable memories.

High-confidence insights are stored as GLOBAL FACT/EXPERIENCE memories
so the orchestrator and agents can benefit from them in future executions.
"""

from __future__ import annotations

import logging

from core.models import (
    LearningInsight,
    InsightType,
    MemoryEntry,
    MemoryScope,
    MemoryType,
)
from memory.manager import MemoryManager

logger = logging.getLogger(__name__)


class LearningOptimizer:
    """
    Convert LearningInsight objects into MemoryEntry objects and
    format them as hints for the orchestrator and agents.
    """

    def __init__(self, memory_manager: MemoryManager) -> None:
        self.memory_manager = memory_manager

    def optimize(
        self,
        insights: list[LearningInsight],
        confidence_threshold: float = 0.7,
    ) -> list[MemoryEntry]:
        """Convert insights above confidence threshold into stored memories."""
        entries: list[MemoryEntry] = []

        for insight in insights:
            if insight.confidence < confidence_threshold:
                continue

            memory_type = self._insight_to_memory_type(insight)
            scope = self._insight_to_scope(insight)
            agent_type = self._insight_to_agent_type(insight)
            content = self._format_insight_content(insight)

            try:
                entry = self.memory_manager.store_learning(
                    agent_type=agent_type,
                    content=content,
                    memory_type=memory_type,
                    scope=scope,
                    keywords=self._extract_insight_keywords(insight),
                )
                entries.append(entry)
            except ValueError:
                logger.debug(
                    "Skipping insight %s: content too long", insight.id,
                )

        logger.info(
            "Optimized %d/%d insights into memories",
            len(entries), len(insights),
        )
        return entries

    def get_planning_hints(self, requirement: str = "") -> str:
        """Format relevant insights as planning hints for the orchestrator."""
        # Get GLOBAL insights relevant to planning
        memories = self.memory_manager.store.search(
            query=f"planning recommendation {requirement}",
            scope=MemoryScope.GLOBAL,
            limit=5,
        )

        if not memories:
            return ""

        lines = ["## Learned Insights for Planning"]
        for mem in memories:
            tag = "AVOID" if "anti_pattern" in str(mem.metadata) else "TIP"
            lines.append(f"- [{tag}] {mem.content}")

        return "\n".join(lines) + "\n"

    def get_agent_hints(
        self,
        agent_type: str,
        task: str = "",
    ) -> str:
        """Format relevant insights for a specific agent."""
        # Get PRIVATE + GLOBAL memories for this agent
        memories = self.memory_manager.get_context_for_agent(
            agent_type=agent_type,
            task_description=f"learning hints {task}",
        )

        # Filter to only learning-sourced memories (those with specific keywords)
        learning_memories = [
            m for m in memories
            if any(kw in m.keywords for kw in [
                "recommendation", "anti_pattern", "pattern", "performance",
            ])
        ]

        if not learning_memories:
            return ""

        lines = [f"## Performance Notes for {agent_type}"]
        for mem in learning_memories[:3]:
            lines.append(f"- {mem.content}")

        return "\n".join(lines) + "\n"

    # -- Helpers --

    def _insight_to_memory_type(self, insight: LearningInsight) -> MemoryType:
        if insight.insight_type == InsightType.ANTI_PATTERN:
            return MemoryType.EXPERIENCE
        return MemoryType.FACT

    def _insight_to_scope(self, insight: LearningInsight) -> MemoryScope:
        if insight.applies_to:
            return MemoryScope.PRIVATE
        return MemoryScope.GLOBAL

    def _insight_to_agent_type(self, insight: LearningInsight) -> str:
        if insight.applies_to:
            return insight.applies_to[0]
        return "shared"

    def _format_insight_content(self, insight: LearningInsight) -> str:
        """Format insight as a concise memory string."""
        prefix = {
            InsightType.PATTERN: "Pattern:",
            InsightType.RECOMMENDATION: "Recommendation:",
            InsightType.ANTI_PATTERN: "Avoid:",
        }.get(insight.insight_type, "")

        content = f"{prefix} {insight.description}"
        # Truncate to max content length
        max_len = self.memory_manager.config.max_content_length
        if len(content) > max_len:
            content = content[:max_len - 3] + "..."
        return content

    def _extract_insight_keywords(self, insight: LearningInsight) -> list[str]:
        """Extract keywords from insight for memory search."""
        keywords = [
            insight.insight_type.value,
            insight.category.value,
        ]
        # Add key tokens from description
        from memory.manager import _extract_keywords
        keywords.extend(_extract_keywords(insight.description, max_keywords=3))
        return keywords
