"""
Prompt registry: loads prompt templates from external files.

Supports:
- Loading prompts from files on disk (orchestrator/prompts/*.md)
- Fallback to built-in defaults if files are unavailable
- Version tracking via content hash for audit/logging
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


_PROMPTS_DIR = Path(__file__).parent

# Built-in defaults — used when prompt files are not found on disk.
# These match the content of the .md files and are the source of truth
# for the prompt content. The .md files can be edited to customize prompts
# without changing Python code.
_BUILTIN_PROMPTS: dict[str, str] = {}


def _load_builtin(name: str) -> str:
    """Load a built-in prompt from the prompts directory."""
    prompt_file = _PROMPTS_DIR / f"{name}.md"
    try:
        return prompt_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ValueError(
            f"Prompt file not found: {prompt_file}. "
            f"Available prompts: {list(_PROMPTS_DIR.glob('*.md'))}"
        )


def _content_hash(text: str) -> str:
    """Return first 8 chars of SHA-256 hash of text."""
    return hashlib.sha256(text.encode()).hexdigest()[:8]


class PromptRegistry:
    """
    Loads and caches prompt templates.

    Usage:
        registry = PromptRegistry()
        prompt = registry.load("planning")
        # prompt is the planning template string

    Custom prompts can be placed in orchestrator/prompts/*.md.
    If a file is missing, raises ValueError.
    """

    def __init__(self, prompts_dir: Path | None = None):
        self._dir = prompts_dir or _PROMPTS_DIR
        self._cache: dict[str, tuple[str, str]] = {}  # name -> (content, hash)

    def load(self, name: str) -> str:
        """Load a prompt template by name.

        Args:
            name: Prompt name (without .md extension), e.g. "planning"

        Returns:
            The prompt template string.

        Raises:
            ValueError: If the prompt file is not found.
        """
        if name in self._cache:
            return self._cache[name][0]

        prompt_file = self._dir / f"{name}.md"
        try:
            content = prompt_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise ValueError(
                f"Prompt file not found: {prompt_file}. "
                f"Available prompts: {list(self._dir.glob('*.md'))}"
            )
        content_hash = _content_hash(content)
        self._cache[name] = (content, content_hash)
        return content

    def get_hash(self, name: str) -> str:
        """Get the content hash of a loaded prompt."""
        if name not in self._cache:
            self.load(name)
        return self._cache[name][1]

    def get_metadata(self) -> dict[str, Any]:
        """Get metadata about all loaded prompts (for session/run logging)."""
        return {
            "prompts_dir": str(self._dir),
            "loaded": {
                name: {"hash": h, "length": len(c)}
                for name, (c, h) in self._cache.items()
            },
        }

    def available_prompts(self) -> list[str]:
        """List available prompt names."""
        return [p.stem for p in self._dir.glob("*.md")]


# Module-level convenience instance
_default_registry: PromptRegistry | None = None


def get_prompt_registry() -> PromptRegistry:
    """Get the default PromptRegistry instance."""
    global _default_registry
    if _default_registry is None:
        _default_registry = PromptRegistry()
    return _default_registry
