"""Consolidated error classification (#501).

Single source of truth for mapping exceptions and error strings
to canonical categories. Replaces 3 separate implementations
across control_plane/.
"""
from __future__ import annotations

from core.exceptions import (
    AgentExecutionError,
    BackendError,
    ConfigurationError,
    MCPError,
    MemoryStoreError,
    NodeTimeoutError,
    RateLimitError,
)


def classify_error(error: str | BaseException) -> str:
    """Classify an error into a canonical category.

    Accepts both string error messages and exception objects.
    Prefers isinstance checks against structured exception types,
    falls back to string pattern matching for legacy errors.
    """
    # Structured exception types (most reliable)
    if isinstance(error, RateLimitError):
        return "rate_limit"
    if isinstance(error, NodeTimeoutError):
        return "timeout"
    if isinstance(error, ConfigurationError):
        return "configuration_error"
    if isinstance(error, BackendError):
        return "backend_error"
    if isinstance(error, AgentExecutionError):
        return "agent_error"
    if isinstance(error, MCPError):
        return "mcp_error"
    if isinstance(error, MemoryStoreError):
        return "memory_error"

    # Extract string for pattern matching
    if isinstance(error, BaseException):
        name = type(error).__name__
        msg = str(error).lower()
        combined = (name + " " + msg).lower()
    else:
        combined = error.lower()

    # Rate limit patterns
    if any(s in combined for s in (
        "ratelimiterror", "429", "rate_limit", "rate limit",
    )):
        return "rate_limit"

    # Timeout
    if "nodetimeouterror" in combined:
        return "timeout"
    if "timeout" in combined or "timed out" in combined:
        return "timeout"

    # Coverage
    if "coverage" in combined and (
        "below target" in combined
        or "could not be verified" in combined
        or "not verified" in combined
    ):
        return "coverage_low"

    # Eval — "evaluation failed", "evaluator", "eval_" prefix (eval_score, eval_result)
    if "evaluation failed" in combined or "evaluator" in combined or "eval_" in combined:
        return "eval_failed"

    # Import / naming
    if any(s in combined for s in (
        "importerror", "modulenotfounderror", "cannot import",
    )):
        return "naming_mismatch"

    # Runtime errors
    if any(s in combined for s in (
        "runtimeerror", "attributeerror", "keyerror",
    )):
        return "runtime_error"

    # Guardrail / blocked
    if any(s in combined for s in ("guardrail", "blocked", "permission")):
        return "tool_blocked"

    # Watchdog
    if "watchdog" in combined or "killed by watchdog" in combined:
        return "watchdog"

    return "unknown"
