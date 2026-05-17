"""Bash command validation and path resolution for tool execution (#515).

Extracted from ToolRegistry for maintainability.
"""
from __future__ import annotations

import re


# Deny patterns for bash command validation (#493).
_DENY_PATTERNS: list[str] = [
    # Destructive filesystem
    r"rm\s+(-[a-z]*f[a-z]*\s+/|-[a-z]*f[a-z]*\s+/*)",
    r"rm\s+-[a-z]*[rf][a-z]*\s+/",
    r"rm\s+(-[rf]\s+)+.*/(etc|usr|var|home|boot|sys|proc)",
    r"r\s*m\s+.*-[a-z]*r[a-z]*f",  # quoted obfuscation
    r"chmod\s+-[a-z]*r\s+(777|a+x|u\+s)",
    r"chown\s+.*:.*\s+/",
    r"dd\s+.*of=/dev/",
    r"mkfs",
    r"shred\s+/",
    r">\s*/dev/sd",
    # System control
    r"\bshutdown\b",
    r"\breboot\b",
    r"\binit\s+[06]",
    r"\b(systemctl|service)\s+(stop|disable|mask)\s+",
    # Fork bomb
    r":\(\)\{.*:\|:&",
    # Reverse shells / network exfiltration
    r"/dev/tcp/",
    r"/dev/udp/",
    r"nc\s+.*-[a-z]*e[a-z]*\s",
    r"ncat\s+.*-[a-z]*e[a-z]*\s",
    r"bash\s+-i\s+",
    r"\b(curl|wget)\s+.*\|\s*(ba)?sh\b",
    r"\b(curl|wget)\s+.*-d\s+@",
    r"\b(curl|wget)\s+.*--data\b.*@\.?",
    # Credential / secret access
    r"/etc/shadow",
    r"/etc/passwd",
    r"\.ssh/id_[rd]sa",
    r"\.ssh/id_ed25519",
    r"\.aws/credentials",
    r"\.aws/config",
    r"\.env\b",
    r"\.gitconfig",
    r"\.netrc",
    # Environment variable dump
    r"\b(print)?env\b(?!\s+PATH\b)(?!\s+HOME\b)",
    r"\bexport\b.*>\s",
    # Privilege escalation
    r"\bsudo\s+",
    r"\bsu\s+",
    r"\bpkexec\b",
    # Package installation (supply chain risk)
    r"\bpip\s+install\s+.*(--user|-e)\b",
    r"\bnpm\s+install\s+-g\b",
    r"\bcargo\s+install\b",
]


def validate_bash_command(command: str) -> str | None:
    """Validate bash command against deny patterns (#493).

    Checks for dangerous operations including destructive filesystem
    commands, network exfiltration, reverse shells, and privilege
    escalation. Uses regex for robust matching against obfuscation.

    Returns the matched pattern if blocked, None if allowed.
    """
    normalized = command.lower().strip()

    # Remove common obfuscation: quotes, backslashes, $'' syntax
    cleaned = re.sub(r"[\'\"\\]", "", normalized)
    # Collapse whitespace
    cleaned = re.sub(r"\s+", " ", cleaned)

    for pattern in _DENY_PATTERNS:
        if re.search(pattern, cleaned):
            return pattern
    return None
