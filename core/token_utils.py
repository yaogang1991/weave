"""Low-level token estimation utilities.

Extracted from orchestrator.llm_utils to break the core → orchestrator
circular dependency (#1087).
"""

# Conservative chars-per-token estimate.
CHARS_PER_TOKEN = 3.5


def estimate_tokens(text: str) -> int:
    """Rough token estimate: chars / 3.5."""
    return int(len(text) / CHARS_PER_TOKEN)
