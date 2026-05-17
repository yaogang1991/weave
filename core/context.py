"""
ContextManager — proactive context compaction for agent message loops.

When the context window exceeds a threshold, summarises early messages via
LLM and replaces them with a compact summary, preserving recent tool
exchanges for continuity.

Integrates with AgentWorker._truncate_messages (#480).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Default: trigger compaction at 60% of context window.
_COMPACT_THRESHOLD = 0.60

# Number of recent messages to preserve (never compacted).
_KEEP_RECENT = 20

# Maximum tool results to keep with full content before clearing.
_KEEP_TOOL_RESULTS = 5


class ContextManager:
    """Manage context window via proactive compaction (#480).

    Replaces the simple truncation in AgentWorker._truncate_messages with:
    1. LLM-generated summary of early conversation
    2. Stale tool result clearing
    3. Threshold-based triggering (60% of max_tokens)
    """

    def __init__(
        self,
        max_tokens: int = 180_000,
        compact_threshold: float = _COMPACT_THRESHOLD,
        keep_recent: int = _KEEP_RECENT,
    ) -> None:
        self.max_tokens = max_tokens
        self.compact_threshold = compact_threshold
        self.keep_recent = keep_recent

    def should_compact(self, messages: list[dict]) -> bool:
        """Check whether context exceeds compaction threshold."""
        return self.estimate_tokens(messages) > self.max_tokens * self.compact_threshold

    def estimate_tokens(self, messages: list[dict]) -> int:
        """Estimate token count with CJK-aware character counting.

        Delegates to AgentWorker._estimate_tokens for consistency,
        but duplicated here to avoid circular imports.
        """
        total_tokens = 0
        for m in messages:
            content = str(m.get("content", ""))
            cjk = sum(1 for c in content if '\u4e00' <= c <= '\u9fff')
            other = len(content) - cjk
            total_tokens += cjk // 2 + other // 4
            for tc in m.get("tool_calls", []):
                arg_str = str(tc.get("arguments", {}))
                cjk_a = sum(1 for c in arg_str if '\u4e00' <= c <= '\u9fff')
                other_a = len(arg_str) - cjk_a
                total_tokens += cjk_a // 2 + other_a // 4
        return max(total_tokens, 1)

    def compact(
        self,
        messages: list[dict],
        llm_client: Any,
    ) -> list[dict]:
        """Compact messages by summarising early conversation.

        Preserves:
        - System prompt (first message)
        - Recent N messages (last `keep_recent`)

        Replaces everything in between with an LLM-generated summary.
        """
        if len(messages) <= self.keep_recent + 1:
            # Not enough messages to compact
            return messages

        system = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        if len(non_system) <= self.keep_recent:
            return messages

        # Split into early (to summarise) and recent (to keep)
        early = non_system[:-self.keep_recent]
        recent = non_system[-self.keep_recent:]

        # Build summary from early messages
        summary = self._summarise(early, llm_client)

        # Clear stale tool results from recent messages
        recent = self._clear_stale_tool_results(recent)

        result = system[:]
        if summary:
            result.append({
                "role": "user",
                "content": f"[Context summary from earlier conversation]\n{summary}",
            })
        result.extend(recent)

        logger.info(
            "Compacted context: %d early messages → summary, kept %d recent "
            "(%d total → %d total)",
            len(early), len(recent),
            len(messages), len(result),
        )
        return result

    def _summarise(self, messages: list[dict], llm_client: Any) -> str:
        """Generate a summary of early conversation messages."""
        # Build a condensed text representation of early messages
        parts: list[str] = []
        for m in messages:
            role = m.get("role", "unknown")
            content = str(m.get("content", ""))[:500]  # Truncate long content
            if role == "tool":
                # Condense tool results
                content = content[:200]
            parts.append(f"[{role}] {content}")

        conversation_text = "\n".join(parts)
        # Cap the input to avoid re-bloating
        if len(conversation_text) > 8000:
            conversation_text = conversation_text[:8000] + "\n... (truncated)"

        try:
            response = llm_client.call(
                messages=[{
                    "role": "user",
                    "content": (
                        "Summarize the following conversation context concisely. "
                        "Focus on: task goals, progress made, files created/modified, "
                        "key decisions, and any pending work.\n\n"
                        f"{conversation_text}"
                    ),
                }],
                tools=[],
            )
            summary = response.get("content", "")
            if summary:
                return summary[:2000]  # Cap summary length
        except Exception as exc:
            logger.warning("Context compaction summary failed: %s", exc)

        # Fallback: extract key points without LLM
        return self._extract_key_points(messages)

    @staticmethod
    def _extract_key_points(messages: list[dict]) -> str:
        """Fallback: extract key points from messages without LLM."""
        files_mentioned: set[str] = set()
        key_decisions: list[str] = []

        for m in messages:
            content = str(m.get("content", ""))
            # Track file paths mentioned
            for word in content.split():
                if word.endswith(".py") or word.endswith(".ts"):
                    files_mentioned.add(word.strip("`'\";,"))
            # Track error messages
            if "Error:" in content or "error:" in content:
                key_decisions.append(content[:200])

        parts: list[str] = ["[Auto-extracted context summary]"]
        if files_mentioned:
            parts.append(f"Files involved: {', '.join(sorted(files_mentioned)[:20])}")
        if key_decisions:
            parts.append("Errors encountered: " + key_decisions[-1][:500])
        return "\n".join(parts)

    @staticmethod
    def _clear_stale_tool_results(
        messages: list[dict],
        keep_last_n: int = _KEEP_TOOL_RESULTS,
    ) -> list[dict]:
        """Clear content of old tool results, keeping the most recent ones."""
        # Find indices of tool messages
        tool_indices = [
            i for i, m in enumerate(messages)
            if m.get("role") == "tool"
        ]

        if len(tool_indices) <= keep_last_n:
            return messages

        # Tool messages to clear
        stale_indices = set(tool_indices[:-keep_last_n])

        result = []
        for i, m in enumerate(messages):
            if i in stale_indices:
                # Replace with placeholder to preserve message ordering
                result.append({"role": "tool", "content": "[cleared]"})
            else:
                result.append(m)
        return result
