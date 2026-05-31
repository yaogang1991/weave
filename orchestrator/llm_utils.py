"""Token/size management and JSON extraction utilities for orchestrator.

Extracted from IntelligentOrchestrator for maintainability (#444).
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)


# Known model context windows (in tokens).
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-sonnet-4-6": 200_000,
    "claude-opus-4-6": 200_000,
    "claude-haiku-4-5": 200_000,
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "o1": 200_000,
    "o3": 200_000,
    "o4-mini": 200_000,
    "kimi": 262_144,
    "moonshot": 262_144,
}
DEFAULT_CONTEXT_WINDOW = 200_000
# Conservative chars-per-token estimate.
CHARS_PER_TOKEN = 3.5
# Anthropic API total message size limit (bytes).
MAX_MESSAGE_BYTES = 2_097_152  # 2 MiB
PRUNE_THRESHOLD = 0.60  # 1.2 MiB


def estimate_tokens(text: str) -> int:
    """Rough token estimate: chars / 3.5."""
    return int(len(text) / CHARS_PER_TOKEN)


def get_context_window(model: str) -> int:
    """Get the context window for a model name."""
    for key, window in MODEL_CONTEXT_WINDOWS.items():
        if key in model:
            return window
    return DEFAULT_CONTEXT_WINDOW


def truncate_requirement_if_needed(
    requirement: str,
    system_prompt: str,
    project_context: str | None,
    model: str,
) -> str:
    """Truncate requirement if the combined prompt exceeds context window (#417).

    Reserves 50% of the window for the model's response.
    Returns the (possibly truncated) requirement.
    """
    system_tokens = estimate_tokens(system_prompt)
    context_tokens = estimate_tokens(project_context or "")
    requirement_tokens = estimate_tokens(requirement)

    total_estimated = system_tokens + context_tokens + requirement_tokens
    window = get_context_window(model)
    max_input_tokens = window // 2

    if total_estimated <= max_input_tokens:
        return requirement

    overhead_tokens = system_tokens + context_tokens
    remaining_tokens = max_input_tokens - overhead_tokens

    if remaining_tokens <= 0:
        logger.warning(
            "System prompt + context (%d tokens) already exceeds "
            "half the context window (%d tokens). Sending requirement "
            "as-is — token limit error is likely.",
            overhead_tokens, max_input_tokens,
        )
        return requirement

    max_chars = int(remaining_tokens * CHARS_PER_TOKEN)
    logger.warning(
        "Requirement too long (%d chars, ~%d tokens). "
        "Truncating to %d chars to fit context window (%d tokens).",
        len(requirement), requirement_tokens, max_chars, window,
    )

    truncated = requirement[:max_chars]
    last_boundary = truncated.rfind("\n\n")
    if last_boundary > max_chars // 2:
        truncated = truncated[:last_boundary]

    truncated += (
        "\n\n[NOTE: The original requirement was truncated from "
        f"{len(requirement)} to {len(truncated)} chars to fit the "
        f"model's context window. Focus on the most critical parts "
        f"and produce a minimal viable plan.]"
    )
    return truncated


def estimate_messages_bytes(messages: list[dict]) -> int:
    """Estimate total byte size of messages payload (UTF-8)."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        total += len(content.encode("utf-8", errors="replace"))
        total += 50
    return total


def prune_messages_for_size(messages: list[dict]) -> list[dict]:
    """Prune messages to stay within the Anthropic 2 MiB limit (#419).

    Strategy (progressively aggressive):
    1. Keep system prompt and last user message intact.
    2. Truncate intermediate assistant messages to 500 chars.
    3. Truncate user messages to 2000 chars.
    4. Drop intermediate messages, keep only first + last.
    5. Hard cap: truncate last user message if still over limit.
    """
    max_bytes = int(MAX_MESSAGE_BYTES * PRUNE_THRESHOLD)
    current = estimate_messages_bytes(messages)

    pruned = [dict(m) for m in messages]

    if current > max_bytes:
        logger.warning(
            "Messages payload %d bytes exceeds %d byte threshold — pruning.",
            current, max_bytes,
        )

        # Pass 1: truncate assistant messages
        for i in range(1, len(pruned) - 1):
            if pruned[i].get("role") == "assistant":
                content = pruned[i].get("content", "")
                if len(content) > 500:
                    pruned[i] = {
                        "role": "assistant",
                        "content": (
                            content[:500]
                            + "\n... (truncated for message size limit)"
                        ),
                    }

        current = estimate_messages_bytes(pruned)
        if current <= max_bytes:
            return pruned

        # Pass 2: truncate user messages
        for i in range(1, len(pruned)):
            if pruned[i].get("role") == "user":
                content = pruned[i].get("content", "")
                if len(content) > 2000:
                    pruned[i] = dict(pruned[i])
                    pruned[i]["content"] = (
                        content[:2000]
                        + "\n... (truncated for message size limit)"
                    )

        current = estimate_messages_bytes(pruned)
        if current <= max_bytes:
            return pruned

        # Pass 3: drop intermediate messages
        if len(pruned) > 2:
            pruned = [pruned[0], pruned[-1]]
            logger.warning(
                "Dropped intermediate messages — keeping only system + last user."
            )

        current = estimate_messages_bytes(pruned)
        if current <= max_bytes:
            return pruned

        # Pass 4: hard cap
        budget = max_bytes - 200
        system_size = len(pruned[0].get("content", "").encode("utf-8", errors="replace"))
        last_size = len(pruned[-1].get("content", "").encode("utf-8", errors="replace"))

        if system_size > budget // 2 and len(pruned) > 1:
            half = budget // 2
            pruned[0] = dict(pruned[0])
            sys_content = pruned[0]["content"]
            sys_bytes = sys_content.encode("utf-8", errors="replace")
            pruned[0]["content"] = (
                sys_bytes[:half].decode("utf-8", errors="replace")
                + "\n... (system prompt truncated for message size limit)"
            )
            remaining = budget - len(pruned[0]["content"].encode("utf-8", errors="replace"))
            if len(pruned) > 1 and last_size > remaining:
                pruned[-1] = dict(pruned[-1])
                last_bytes = pruned[-1]["content"].encode("utf-8", errors="replace")
                pruned[-1]["content"] = (
                    last_bytes[:remaining].decode("utf-8", errors="replace")
                    + "\n... (truncated for message size limit)"
                )
        else:
            for i in range(len(pruned)):
                if pruned[i].get("content") and len(
                    pruned[i]["content"].encode("utf-8", errors="replace")
                ) > budget:
                    pruned[i] = dict(pruned[i])
                    content_bytes = pruned[i]["content"].encode("utf-8", errors="replace")
                    pruned[i]["content"] = (
                        content_bytes[:budget].decode("utf-8", errors="replace")
                        + "\n... (truncated for message size limit)"
                    )
                    break

        final_size = estimate_messages_bytes(pruned)
        if final_size > MAX_MESSAGE_BYTES:
            logger.error(
                "After aggressive pruning, messages still %d bytes (limit %d). "
                "This will likely fail at the API.",
                final_size, MAX_MESSAGE_BYTES,
            )

    return pruned


def prune_messages_for_tokens(
    messages: list[dict],
    model: str,
) -> list[dict]:
    """Token-based pruning: truncate largest message if token count exceeds half context window."""
    window = get_context_window(model)
    max_input_tokens = window // 2
    total_tokens = sum(estimate_tokens(m.get("content", "")) for m in messages)

    if total_tokens <= max_input_tokens:
        return messages

    logger.warning(
        "Messages ~%d tokens exceed half of context window (%d). "
        "Token-pruning to fit.",
        total_tokens, max_input_tokens,
    )

    pruned = [dict(m) for m in messages]
    char_budget = int(max_input_tokens * CHARS_PER_TOKEN)
    for i in range(len(pruned)):
        content = pruned[i].get("content", "")
        if len(content) > char_budget // 2:
            pruned[i] = dict(pruned[i])
            pruned[i]["content"] = (
                content[:char_budget // 2]
                + "\n... (truncated for token limit)"
            )
            break

    return pruned


def is_response_truncated(content: str) -> bool:
    """Detect if planner JSON response was truncated (#621).

    Checks for unclosed braces in what looks like a JSON object.
    Used by both Planner and IntelligentOrchestrator to detect
    incomplete LLM responses that need repair or retry.
    """
    if not content:
        return False
    stripped = content.strip()
    if not stripped.startswith("{"):
        return False
    if stripped.endswith("}"):
        return False
    return stripped.count("{") > stripped.count("}")


def extract_json(text: str) -> dict | None:
    """Extract JSON from LLM response (handles markdown code blocks).

    Collects candidate substrings from multiple strategies, tries
    json.loads on each, and only attempts repair on truly truncated
    candidates (unclosed braces at end-of-text).

    Returns None when no valid JSON can be extracted.
    """
    text = text.strip()
    candidates: list[str] = []

    # Strategy 1: JSON inside ```json ... ``` blocks
    json_block_match = re.search(r"```json\s*\n(.*?)```", text, re.DOTALL)
    if json_block_match:
        candidates.append(json_block_match.group(1).strip())

    # Strategy 2: JSON inside generic ``` ... ``` blocks
    generic_block_match = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
    if generic_block_match:
        candidate = generic_block_match.group(1).strip()
        if candidate.startswith("{") or candidate.startswith("["):
            candidates.append(candidate)

    # Strategy 3: First top-level JSON object via brace matching
    brace_depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == '{':
            if brace_depth == 0:
                start = i
            brace_depth += 1
        elif ch == '}':
            brace_depth -= 1
            if brace_depth == 0 and start is not None:
                candidates.append(text[start:i + 1])
                start = None

    # Strategy 4: Truncated JSON (unclosed braces at end-of-text)
    if start is not None and brace_depth > 0:
        candidates.append(repair_truncated_json(text[start:], brace_depth))

    for candidate in candidates:
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except (json.JSONDecodeError, ValueError):
            continue

    return None


def repair_truncated_json(text: str, brace_depth: int) -> str:
    """Attempt to close a truncated JSON object by appending missing
    closing quotes, brackets, and braces (#561).

    Handles common truncation patterns:
    - Unclosed string values
    - Unclosed arrays (``]``)
    - Missing ``]`` before ``}``
    - Truncated mid-field (strips trailing incomplete key-value pair)
    """
    # Close unclosed string first
    quote_count = text.count('"') - text.count('\\"')
    in_string = quote_count % 2 == 1
    if in_string:
        text += '"'

    stripped = text.rstrip()

    # Characters that indicate a complete JSON value at the end
    _COMPLETE_VALUE_ENDS = frozenset({
        '}', ']', '"', "'",          # structural
        '0', '1', '2', '3', '4',     # numbers
        '5', '6', '7', '8', '9',
        'e', 'E',                      # number exponent
        'l',                           # null / bool (l from null/false/true)
    })

    if stripped:
        last_char = stripped[-1]

        if last_char == ':':
            # Truncated right after key: — insert empty value
            text = stripped + '""'
        elif last_char == ',':
            # Truncated after comma — remove trailing comma
            text = stripped
        elif last_char in _COMPLETE_VALUE_ENDS:
            # Last char is a complete value terminator — just close structures
            text = stripped
        elif last_char not in _COMPLETE_VALUE_ENDS:
            # Truncated mid-value or mid-key (alphabetic, etc.)
            last_comma = stripped.rfind(",")
            last_colon = stripped.rfind(":")
            last_open_bracket = stripped.rfind("[")

            if last_open_bracket > max(last_comma, last_colon):
                # Inside an array — close it
                text = stripped + "]"
            elif last_comma > last_colon:
                # After a comma — remove incomplete trailing element
                text = stripped[:last_comma]
            elif last_colon >= 0:
                # Mid-value — replace with empty string
                text = stripped[:last_colon + 1] + '""'

    # Close unclosed brackets
    bracket_depth = text.count("[") - text.count("]")
    if bracket_depth > 0:
        text += "]" * bracket_depth

    # Close unclosed braces
    text += "}" * brace_depth

    return text
