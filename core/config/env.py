"""Environment variable helpers for config defaults."""
from __future__ import annotations

import json
import logging
import os
import warnings
from pathlib import Path

logger = logging.getLogger(__name__)


def _get_non_interactive_env() -> str:
    """Read non-interactive env var with backwards compatibility (#543)."""
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


_CLAUDE_ENV = _load_claude_settings()


def infer_provider(model: str, default: str = "anthropic") -> str:
    """Infer LLM provider from model name."""
    if model.startswith("gpt") or model.startswith("chatgpt"):
        return "openai"
    if len(model) > 1 and model[0] == "o" and model[1].isdigit():
        return "openai"
    if model.startswith("claude"):
        return "anthropic"
    return default
