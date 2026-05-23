"""IssueRanker -- LLM-based issue prioritization with fallback."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from integrations.models import NormalizedIssue

logger = logging.getLogger(__name__)


class IssueRanker:
    """Ranks NormalizedIssue list by priority using LLM."""

    def __init__(self, llm_config: Any = None) -> None:
        self._llm_config = llm_config

    async def rank(self, issues: list[NormalizedIssue]) -> list[NormalizedIssue]:
        if len(issues) <= 1:
            return list(issues)
        try:
            return await self._llm_rank(issues)
        except Exception:
            logger.warning("LLM ranking failed, falling back to chronological")
            return sorted(issues, key=lambda i: i.created_at or datetime.min)

    async def _llm_rank(self, issues: list[NormalizedIssue]) -> list[NormalizedIssue]:
        from core.llm_client import LLMClient

        client = LLMClient(
            api_key=self._llm_config.api_key,
            model=getattr(self._llm_config, "model", "claude-sonnet-4-6"),
            provider=getattr(self._llm_config, "provider", "anthropic"),
            base_url=getattr(self._llm_config, "base_url", None),
        )

        issue_list = "\n".join(
            f"#{i.number}: {i.title} (created: {i.created_at})"
            for i in issues
        )
        prompt = (
            "Rank these GitHub issues by priority for automated resolution.\n"
            "Consider: urgency, independence (can be solved in isolation), clarity.\n\n"
            f"{issue_list}\n\n"
            "Return a JSON array of issue numbers in priority order (highest first).\n"
            "Example: [42, 17, 5]"
        )

        response = await client.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
        )
        text = response.get("content", "")
        if isinstance(text, list):
            text = " ".join(b.get("text", "") for b in text if isinstance(b, dict))

        numbers = json.loads(text.strip())
        issue_map = {i.number: i for i in issues}
        return [issue_map[n] for n in numbers if n in issue_map]
