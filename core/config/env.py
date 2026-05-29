"""Environment variable loading and shared utilities."""

from __future__ import annotations

import json
import os
import warnings
from pathlib import Path

import logging

logger = logging.getLogger(__name__)


def _get_non_interactive_env() -> str:
    """Read non-interactive env var with backwards compatibility (#543).

    Supports both WEAVE_NON_INTERACTIVE (current) and
    HARNESS_NON_INTERACTIVE (deprecated). Emits DeprecationWarning
    when only the old name is set.
    """
    new_val = os.environ.get("WEAVE_NON_INTERACTIVE")
    old_val = os.environ.get("HARNESS_NON_INTERACTIVE")

    if new_val is not None:
        return new_val
    if old_val is not None:
        warnings.warn(
            "HARNESS_NON_INTERACTIVE is deprecated, use WEAVE_NON_INTERACTIVE",
            DeprecationWarning,
            stacklevel=3,
        )
        return old_val
    return ""


def _load_claude_settings() -> dict[str, str]:
    """Load env vars from ~/.claude/settings-kimi.json if present."""
    settings_path = Path.home() / ".claude" / "settings-kimi.json"
    if settings_path.exists():
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("env", {})
        except Exception as exc:
            logger.debug("Failed to load claude settings: %s", exc)
    return {}


# Cache so we don't re-read the file for every field default.
_CLAUDE_ENV = _load_claude_settings()


def infer_provider(model: str, default: str = "anthropic") -> str:
    """Infer LLM provider from model name.

    Handles known prefixes: gpt*, chatgpt*, o-series (o1, o3, o4, etc.),
    and claude*.
    """
    if model.startswith("gpt") or model.startswith("chatgpt"):
        return "openai"
    # o-series models: o1, o3, o4, etc. (starts with 'o' followed by digit)
    if len(model) > 1 and model[0] == "o" and model[1].isdigit():
        return "openai"
    if model.startswith("claude"):
        return "anthropic"
    return default
