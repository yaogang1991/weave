"""
Project-level configuration for the Weave.

Loads from ``.weave/config.yaml`` in the project root.
Provides operational parameters (runtime, hooks, guardrails, project context)
without touching prompts — the LLM orchestrator handles planning dynamically.

Design rationale:
- Symphony uses WORKFLOW.md for both prompt + config because it has a
  hardcoded state machine. Weave uses an LLM-driven orchestrator, so
  prompts are generated dynamically. This file only needs operational params.
- Each config section is consumed by a different component:
  - runtime -> DAG engine, worker
  - hooks -> BackendManager (subprocess execution)
  - guardrails -> Guardrails policy
  - project_context -> Orchestrator agent (auxiliary info)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class HookConfig(BaseModel):
    """Workspace lifecycle hooks — shell commands executed at each stage.

    Failure semantics (matching Symphony):
    - after_create: fatal to workspace creation
    - before_run: fatal to current attempt
    - after_run: logged and ignored
    - before_remove: logged and ignored
    """

    after_create: str = ""
    before_run: str = ""
    after_run: str = ""
    before_remove: str = ""
    timeout_sec: int = 60


class RuntimeConfig(BaseModel):
    """Runtime parameters consumed by DAG engine and worker."""

    max_turns: int = 50
    max_parallel: int = 3
    turn_timeout_sec: int = 600
    max_retries: int = 3
    base_backoff_sec: float = 1.0
    max_backoff_sec: int = 300  # 5 minutes, matching Symphony default
    backoff_multiplier: float = 2.0


class GuardrailsConfig(BaseModel):
    """Guardrails policy overrides — consumed by guardrails module."""

    denied_commands: list[str] = Field(default_factory=list)
    approval_policy: str = "accept_edits"  # plan/default/accept_edits/auto/dont_ask
    protected_paths: list[str] = Field(
        default_factory=lambda: [
            ".env", ".env.*", "credentials*", "id_rsa", "id_ed25519",
            ".ssh/*", ".gnupg/*", ".git/config",
        ],
    )


class ProjectContext(BaseModel):
    """Project metadata — injected into orchestrator agent as auxiliary context.

    Helps the orchestrator make better DAG plans by knowing the tech stack.
    """

    language: str = ""
    framework: str = ""
    test_runner: str = ""
    conventions: list[str] = Field(default_factory=list)


class ProjectConfig(BaseModel):
    """Top-level project configuration loaded from ``.weave/config.yaml``."""

    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    hooks: HookConfig = Field(default_factory=HookConfig)
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    project_context: ProjectContext = Field(default_factory=ProjectContext)

    @classmethod
    def load(cls, project_path: str | Path | None = None) -> ProjectConfig:
        """Load project config from ``<project_path>/.weave/config.yaml``.

        Returns default config if the file does not exist.
        """
        if project_path is None:
            return cls()

        config_path = Path(project_path) / ".weave" / "config.yaml"
        if not config_path.exists():
            return cls()

        with open(config_path) as f:
            data = yaml.safe_load(f) or {}

        return cls(**data)

    def to_summary(self) -> str:
        """Format project_context fields as a text summary for LLM injection."""
        ctx = self.project_context
        if not any([ctx.language, ctx.framework, ctx.test_runner, ctx.conventions]):
            return ""
        lines: list[str] = []
        if ctx.language:
            lines.append(f"Language: {ctx.language}")
        if ctx.framework:
            lines.append(f"Framework: {ctx.framework}")
        if ctx.test_runner:
            lines.append(f"Test runner: {ctx.test_runner}")
        if ctx.conventions:
            lines.append(f"Conventions: {', '.join(ctx.conventions)}")
        return "\n".join(lines)

    def effective_runtime(self, overrides: dict[str, Any] | None = None) -> RuntimeConfig:
        """Return runtime config with optional overrides applied.

        Useful for merging CLI flags on top of project config.
        """
        if not overrides:
            return self.runtime
        data = self.runtime.model_dump()
        for k, v in overrides.items():
            if v is not None and k in data:
                data[k] = v
        return RuntimeConfig(**data)
