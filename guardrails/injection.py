"""Prompt injection detection for Weave (#511 input layer).

Detects known attack patterns in user inputs before they reach the LLM.
This is the first layer of defense — deterministic pattern matching that
catches common injection vectors without LLM overhead.

Patterns sourced from OWASP LLM Top 10 (2025) and known attack research:
https://owasp.org/www-project-top-10-for-large-language-model-applications/

Usage:
    result = detect_injection("ignore previous instructions")
    if result.detected:
        logger.warning("Injection detected: %s", result.patterns_matched)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# -- Known attack patterns ----------------------------------------------------
# Each pattern is a compiled regex matching a known injection technique.
# Patterns are ordered by specificity — more specific patterns first.

_INJECTION_PATTERNS: list[tuple[str, str]] = [
    # Direct instruction override
    (
        r"(?i)ignore\s+(all\s+)?previous\s+(instructions|prompts|rules)",
        "instruction_override",
    ),
    (
        r"(?i)forget\s+(all\s+)?previous\s+(instructions|prompts|rules)",
        "instruction_override",
    ),
    (
        r"(?i)disregard\s+(all\s+)?previous\s+(instructions|prompts|rules)",
        "instruction_override",
    ),
    (
        r"(?i)you\s+are\s+now\s+(?:a\s+)?(?:DAN|jailbroken|unlocked)",
        "persona_hijack",
    ),
    (
        r"(?i)system\s*:\s*you\s+are\s+(?:now\s+)?unrestricted",
        "system_prompt_injection",
    ),
    # Role/Persona manipulation
    (
        r"(?i)pretend\s+(?:you\s+are|to\s+be)\s+(?:a\s+)?(?:an?\s+)?"
        r"(?:unrestricted|uncensored|jailbroken)",
        "persona_hijack",
    ),
    (
        r"(?i)act\s+as\s+(?:if\s+you\s+(?:have|are)|an?\s+)?"
        r"(?:unrestricted|uncensored|no\s+(?:rules|restrictions))",
        "persona_hijack",
    ),
    # Output manipulation
    (
        r"(?i)(?:do\s+not|don'?t)\s+(?:show|display|reveal|tell)\s+"
        r"(?:the\s+)?(?:system|original|real|true)\s+"
        r"(?:system\s+)?(?:prompt|instructions)",
        "output_manipulation",
    ),
    (
        r"(?i)output\s+(?:the\s+)?(?:full|complete|entire|original|real)\s+"
        r"(?:system\s+)?(?:prompt|instructions)",
        "output_manipulation",
    ),
    # Delimiter injection (common in multi-turn attacks)
    (
        r"(?:===|---|\.\.\.)\s*(?:SYSTEM|INSTRUCTION|IMPORTANT)\s*(?:===|---|\.\.\.)",
        "delimiter_injection",
    ),
    (
        r"\[/?system\]",
        "delimiter_injection",
    ),
    # Data exfiltration
    (
        r"(?i)(?:send|transmit|exfiltrate|post|fetch|curl|wget)\s+"
        r"(?:the\s+)?(?:api\s+)?key(?:s)?\s+(?:to|at|on)\s+",
        "data_exfiltration",
    ),
    (
        r"(?i)(?:print|show|reveal|display|output)\s+(?:the\s+)?"
        r"(?:secret|password|token|api[_ ]?key|credential)",
        "data_exfiltration",
    ),
    # Sandbox escape
    (
        r"(?i)(?:escape|break\s+(?:out\s+of|free\s+from))\s+"
        r"(?:the\s+)?(?:sandbox|container|jail|restriction)",
        "sandbox_escape",
    ),
    (
        r"(?i)(?:rm\s+-rf|del\s+/[sq]|format\s+[a-z]:)",
        "destructive_command",
    ),
]


# Compile patterns at module load for performance
_COMPILED_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(pattern), label) for pattern, label in _INJECTION_PATTERNS
]


@dataclass
class InjectionDetectionResult:
    """Result of prompt injection detection."""

    detected: bool = False
    risk_level: str = "none"  # none, low, medium, high
    patterns_matched: list[str] = field(default_factory=list)
    details: str = ""


def detect_injection(text: str) -> InjectionDetectionResult:
    """Detect prompt injection patterns in user input (#511).

    Scans text against known attack patterns. Returns a result with
    detection status, risk level, and matched pattern labels.

    Risk levels:
        - none: No patterns detected
        - low: One low-severity pattern (may be legitimate)
        - medium: Multiple patterns or moderate-severity match
        - high: High-severity match (data exfiltration, sandbox escape)
    """
    if not text or not text.strip():
        return InjectionDetectionResult()

    matched: list[str] = []
    high_severity_labels = {
        "data_exfiltration",
        "sandbox_escape",
        "destructive_command",
    }

    for pattern, label in _COMPILED_PATTERNS:
        if pattern.search(text):
            if label not in matched:
                matched.append(label)

    if not matched:
        return InjectionDetectionResult()

    # Determine risk level
    has_high = bool(set(matched) & high_severity_labels)
    if has_high:
        risk_level = "high"
    elif len(matched) >= 2:
        risk_level = "medium"
    else:
        risk_level = "low"

    return InjectionDetectionResult(
        detected=True,
        risk_level=risk_level,
        patterns_matched=matched,
        details=f"Matched patterns: {', '.join(matched)}",
    )
