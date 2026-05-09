"""
Knowledge Index — Programmatic access to project specs and ADRs.

Provides structured reading of documentation for LLM prompt injection.
Used by the orchestrator and agents to inject project knowledge into system prompts.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


class KnowledgeIndex:
    """Reads and indexes project documentation for LLM consumption."""

    def __init__(self, docs_root: str | None = None):
        """Initialize with the docs root directory.

        Args:
            docs_root: Path to the docs directory. Defaults to ./docs
                       relative to this file's parent.
        """
        if docs_root is None:
            docs_root = str(Path(__file__).parent)
        self.docs_root = Path(docs_root)
        self._specs: dict[str, str] = {}
        self._adrs: dict[int, str] = {}
        self._config_ref: str = ""
        self._dev_guide: str = ""
        self._loaded = False

    def _ensure_loaded(self) -> None:
        """Lazy-load all documents on first access."""
        if self._loaded:
            return
        self._load_specs()
        self._load_adrs()
        self._load_config_ref()
        self._load_dev_guide()
        self._loaded = True

    def _load_specs(self) -> None:
        """Load all module SPEC documents."""
        specs_dir = self.docs_root / "specs"
        if not specs_dir.exists():
            return
        for f in sorted(specs_dir.glob("*.md")):
            if f.name == "00_TEMPLATE.md":
                continue
            key = f.stem  # e.g., "core_models"
            self._specs[key] = f.read_text(encoding="utf-8")

    def _load_adrs(self) -> None:
        """Load all ADR documents."""
        adrs_dir = self.docs_root / "adrs"
        if not adrs_dir.exists():
            return
        for f in sorted(adrs_dir.glob("*.md")):
            match = re.match(r"(\d{4})", f.name)
            if match:
                num = int(match.group(1))
                self._adrs[num] = f.read_text(encoding="utf-8")

    def _load_config_ref(self) -> None:
        """Load the config reference document."""
        path = self.docs_root / "config_reference.md"
        if path.exists():
            self._config_ref = path.read_text(encoding="utf-8")

    def _load_dev_guide(self) -> None:
        """Load the developer guide document."""
        path = self.docs_root / "dev_guide.md"
        if path.exists():
            self._dev_guide = path.read_text(encoding="utf-8")

    def get_module_spec(self, module_name: str) -> str:
        """Get a specific module's SPEC content.

        Args:
            module_name: Module name (e.g., "core_models", "orchestrator").
                        Underscores or dots accepted.

        Returns:
            SPEC content as string, or empty string if not found.
        """
        self._ensure_loaded()
        key = module_name.replace(".", "_")
        return self._specs.get(key, "")

    def get_adr(self, adr_number: int) -> str:
        """Get a specific ADR by number.

        Args:
            adr_number: ADR number (e.g., 1, 2, 8).

        Returns:
            ADR content as string, or empty string if not found.
        """
        self._ensure_loaded()
        return self._adrs.get(adr_number, "")

    def get_config_reference(self) -> str:
        """Get the full configuration reference.

        Returns:
            Config reference content as string.
        """
        self._ensure_loaded()
        return self._config_ref

    def get_dev_guide(self) -> str:
        """Get the full developer guide.

        Returns:
            Developer guide content as string.
        """
        self._ensure_loaded()
        return self._dev_guide

    def list_available_specs(self) -> list[str]:
        """List all available module SPEC names.

        Returns:
            List of module names (e.g., ["core_models", "orchestrator", ...]).
        """
        self._ensure_loaded()
        return sorted(self._specs.keys())

    def list_available_adrs(self) -> list[int]:
        """List all available ADR numbers.

        Returns:
            Sorted list of ADR numbers.
        """
        self._ensure_loaded()
        return sorted(self._adrs.keys())

    def _estimate_tokens(self, text: str) -> int:
        """Rough token estimate (1 token ≈ 4 chars)."""
        return len(text) // 4

    def _truncate_to_tokens(self, text: str, max_tokens: int) -> str:
        """Truncate text to approximately max_tokens."""
        max_chars = max_tokens * 4
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n... (truncated)"

    def _score_relevance(self, text: str, keywords: list[str]) -> float:
        """Score text relevance against keywords.

        Args:
            text: Text to score.
            keywords: Lowercase keywords from the task description.

        Returns:
            Relevance score (higher = more relevant).
        """
        text_lower = text.lower()
        score = 0.0
        for kw in keywords:
            count = text_lower.count(kw)
            score += count
        return score

    def _extract_keywords(self, text: str) -> list[str]:
        """Extract meaningful keywords from a task description.

        Filters out common stop words and short tokens.

        Args:
            text: Task description text.

        Returns:
            List of lowercase keywords.
        """
        stop_words = {
            "the", "a", "an", "and", "or", "but", "in", "on", "at", "to",
            "for", "of", "with", "by", "from", "is", "are", "was", "were",
            "be", "been", "being", "have", "has", "had", "do", "does", "did",
            "will", "would", "could", "should", "may", "might", "must", "can",
            "this", "that", "these", "those", "it", "its", "i", "me", "my",
            "we", "our", "you", "your", "he", "she", "they", "them",
            "what", "which", "who", "whom", "how", "when", "where", "why",
            "not", "no", "nor", "if", "then", "else", "so", "than", "too",
            "very", "just", "about", "up", "out", "all", "some", "any",
            "build", "add", "create", "make", "get", "set", "use", "using",
            "implement", "need", "want", "like", "new", "also", "into",
        }
        words = re.findall(r"[a-zA-Z_]+", text.lower())
        return [w for w in words if w not in stop_words and len(w) > 2]

    def get_relevant_context(
        self,
        task_description: str,
        max_tokens: int = 4000,
        include_adrs: bool = True,
        include_config: bool = False,
    ) -> str:
        """Get the most relevant documentation context for a task.

        Selects SPECs and ADRs most relevant to the task description,
        staying within a token budget.

        Args:
            task_description: The task or requirement text.
            max_tokens: Maximum approximate tokens to return.
            include_adrs: Whether to include relevant ADRs.
            include_config: Whether to include config reference.

        Returns:
            Formatted context string suitable for prompt injection.
        """
        self._ensure_loaded()
        keywords = self._extract_keywords(task_description)
        sections: list[tuple[float, str, str]] = []  # (score, title, content)

        # Score and collect specs
        for name, content in self._specs.items():
            score = self._score_relevance(content, keywords)
            # Boost if module name keywords match directly
            name_parts = name.replace("_", " ").split()
            for part in name_parts:
                if part in keywords:
                    score += 5.0
            if score > 0:
                title = f"## Module SPEC: {name}"
                sections.append((score, title, content))

        # Score and collect ADRs
        if include_adrs:
            for num, content in self._adrs.items():
                score = self._score_relevance(content, keywords)
                if score > 0:
                    title = f"## ADR {num:04d}"
                    sections.append((score, title, content))

        # Sort by relevance (highest first)
        sections.sort(key=lambda x: x[0], reverse=True)

        # Build output within token budget
        result_parts: list[str] = []
        remaining_tokens = max_tokens

        if include_config and self._config_ref:
            config_section = "## Configuration Reference\n" + self._config_ref
            config_tokens = self._estimate_tokens(config_section)
            if config_tokens < remaining_tokens:
                result_parts.append(config_section)
                remaining_tokens -= config_tokens

        for score, title, content in sections:
            section = f"{title}\n{content}"
            section_tokens = self._estimate_tokens(section)
            if section_tokens > remaining_tokens:
                # Try truncated version
                truncated = self._truncate_to_tokens(
                    content, remaining_tokens - self._estimate_tokens(title) - 50
                )
                section = f"{title}\n{truncated}"
                section_tokens = self._estimate_tokens(section)
            if section_tokens <= remaining_tokens:
                result_parts.append(section)
                remaining_tokens -= section_tokens
            if remaining_tokens <= 200:
                break

        if not result_parts:
            return "# No relevant documentation found for this task."

        return "\n\n---\n\n".join(result_parts)
