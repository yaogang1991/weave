"""Guardrail and permission policy models."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class RiskLevel(int, Enum):
    """Risk classification for operations."""
    LOW = 1       # Read-only, safe
    MEDIUM = 2    # File edits, reversible
    HIGH = 3      # Bash commands, network access
    CRITICAL = 4  # Irreversible, production impact


class PermissionMode(str, Enum):
    """Permission modes."""
    PLAN = "plan"                    # Read-only
    DEFAULT = "default"              # Ask for every action
    ACCEPT_EDITS = "accept_edits"    # Auto-approve file edits
    AUTO = "auto"                    # Classifier-based approval
    DONT_ASK = "dont_ask"            # Only pre-approved tools


class GuardrailPolicy(BaseModel):
    """Policy configuration for guardrails."""
    mode: PermissionMode = PermissionMode.DEFAULT
    allowed_tools: list[str] = Field(default_factory=list)
    denied_tools: list[str] = Field(default_factory=list)
    allowed_commands: list[str] = Field(default_factory=list)
    denied_commands: list[str] = Field(default_factory=list)
    max_bash_duration: int = 120
    max_iterations: int = 50
    auto_approve_read: bool = True
    require_human_on_error: bool = True


class PersonalGuardrailPolicy(GuardrailPolicy):
    """Personal mode guardrail policy.

    LOW/MEDIUM: auto-approve
    HIGH: requires confirmation (or whitelist match)
    CRITICAL: always requires confirmation
    """
    whitelist_patterns: list[str] = Field(default_factory=list)
    # Safe read-only / mkdir commands that should not require approval
    # even in default interactive mode (#380)
    whitelist_commands: list[str] = Field(default_factory=lambda: [
        "mkdir",     # create directories
        "ls",        # list files
        "cat",       # view files
        "touch",     # create empty files
        "pwd",       # print working directory
        "which",     # locate commands
        "echo",      # print text
        "head",      # view file beginning
        "tail",      # view file end
        "wc",        # count lines/words
        "find",      # find files
        "python -m pytest",  # run tests
        "python3 -m pytest", # run tests (alt)
        "pytest",    # run tests (direct)
    ])
    auto_approve_high: bool = False
    confirmation_timeout_sec: int = 300
