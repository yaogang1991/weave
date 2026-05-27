"""ClaudeCodeBackend -- delegates node execution to Claude Code (SDK or CLI).

M4.1 implementation: integrates Claude Code as an external Worker backend
for the Weave DAG orchestration system.

Two execution paths:
1. SDK path: Uses the claude-code Python SDK (if installed).
2. CLI fallback: Shells out to the `claude` CLI with --output-format json.

All vendor-specific code is isolated in this single file.
BuiltinBackend always remains available as the safe fallback.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any

from core.backend_models import BackendContext, BackendResult, BackendStatus
from core.exceptions import BudgetExhaustedError, NodeTimeoutError, RateLimitError
from core.subprocess_runner import run_with_progress
from agent.backends.base import AgentBackend

logger = logging.getLogger(__name__)

# Named constants for truncation limits (#612 #16).
ARTIFACT_CONTENT_LIMIT = 2000
SUMMARY_LIMIT = 500
OUTPUT_PREVIEW_LIMIT = 2000
DEFAULT_CLI_TIMEOUT = 1800

# Valid permission modes (#612 #2).
VALID_PERMISSION_MODES = {"default", "plan", "bypassPermissions"}

__all__ = ["ClaudeCodeBackend", "ClaudeCodeRuntimeConfig"]


class ClaudeCodeRuntimeConfig:
    """Immutable runtime configuration for ClaudeCodeBackend.

    Created once per backend instance from core.config.ClaudeCodeConfig.
    Renamed from ClaudeCodeConfig to avoid collision (#612 #10).
    """

    __slots__ = (
        "_cli_path", "_model", "_max_turns", "_permission_mode",
        "_allowed_tools", "_system_prompt_append", "_max_budget_usd",
        "_timeout_override",
    )

    def __init__(
        self,
        cli_path: str = "claude",
        model: str = "",
        max_turns: int = 0,
        permission_mode: str = "default",
        allowed_tools: list[str] | None = None,
        system_prompt_append: str = "",
        max_budget_usd: float = 0.0,
        timeout_override: int = 0,
    ) -> None:
        self._cli_path = cli_path
        self._model = model
        self._max_turns = max_turns
        if permission_mode not in VALID_PERMISSION_MODES:
            raise ValueError(
                f"permission_mode must be one of {VALID_PERMISSION_MODES}, "
                f"got '{permission_mode}'"
            )
        self._permission_mode = permission_mode
        self._allowed_tools = tuple(allowed_tools) if allowed_tools else ()
        self._system_prompt_append = system_prompt_append
        self._max_budget_usd = max_budget_usd
        self._timeout_override = timeout_override

    @classmethod
    def from_core_config(cls, config: Any) -> ClaudeCodeRuntimeConfig:
        """Create from core.config.ClaudeCodeConfig."""
        return cls(
            cli_path=config.cli_path,
            model=config.model,
            max_turns=config.max_turns,
            permission_mode=config.permission_mode,
            allowed_tools=list(config.allowed_tools) if config.allowed_tools else None,
            system_prompt_append=config.system_prompt_append,
            max_budget_usd=config.max_budget_usd,
            timeout_override=config.timeout_override,
        )

    @property
    def cli_path(self) -> str:
        return self._cli_path

    @property
    def model(self) -> str:
        return self._model

    @property
    def max_turns(self) -> int:
        return self._max_turns

    @property
    def permission_mode(self) -> str:
        return self._permission_mode

    @property
    def allowed_tools(self) -> list[str]:
        return list(self._allowed_tools)

    @property
    def system_prompt_append(self) -> str:
        return self._system_prompt_append

    @property
    def max_budget_usd(self) -> float:
        return self._max_budget_usd

    @property
    def timeout_override(self) -> int:
        return self._timeout_override


class ClaudeCodeBackend(AgentBackend):
    """Executes DAG nodes by delegating to Claude Code.

    Claude Code handles its own OS-level sandboxing (bubblewrap/Seatbelt + gVisor),
    so no SandboxProvider is needed.

    The backend does NOT manage:
    - Workspace isolation (handled by BackendManager)
    - Evaluation (handled by EvaluatorEngine)
    - Retry logic (handled by NodeExecutor)
    - Timeout enforcement (handled by NodeExecutor)
    """

    BACKEND_NAME = "claude_code"

    def __init__(self, config: ClaudeCodeRuntimeConfig) -> None:
        self._config = config
        self._sdk_available: bool | None = None
        self._cli_available: bool | None = None

    @property
    def name(self) -> str:
        return self.BACKEND_NAME

    def get_capabilities(self) -> list[str]:
        return ["planner", "generator", "evaluator"]

    # -- AgentBackend interface -----------------------------------------------

    async def health_check(self) -> bool:
        # Re-check availability each time (#612 #7 — no permanent cache).
        self._sdk_available = None
        self._cli_available = None
        return self._is_sdk_available() or self._is_cli_available()

    async def execute(self, context: BackendContext) -> BackendResult:
        """Execute a node via Claude Code.

        Strategy: SDK first, CLI fallback.
        """
        prompt = self._build_prompt(context)

        if self._is_sdk_available():
            try:
                return await self._execute_via_sdk(context, prompt)
            except Exception as exc:
                logger.warning(
                    "Claude Code SDK failed (%s), falling back to CLI", exc,
                )

        if self._is_cli_available():
            return await self._execute_via_cli(context, prompt)

        return BackendResult(
            status=BackendStatus.FAILED,
            error="Claude Code SDK not installed and CLI not found in PATH",
        )

    # -- SDK path ------------------------------------------------------------

    def _is_sdk_available(self) -> bool:
        if self._sdk_available is not None:
            return self._sdk_available
        try:
            import claude_code_sdk  # noqa: F401
            self._sdk_available = True
        except ImportError:
            self._sdk_available = False
        return self._sdk_available

    async def _execute_via_sdk(
        self, context: BackendContext, prompt: str,
    ) -> BackendResult:
        """Execute via Claude Code Python SDK.

        Uses asyncio-compatible invocation: if the SDK provides an async
        API, await directly. Otherwise, fall back to run_in_executor (#612 #1).
        """
        import claude_code_sdk

        options: dict[str, Any] = {
            "prompt": prompt,
            "cwd": context.workspace_path or ".",
        }
        if self._config.allowed_tools:
            options["allowed_tools"] = self._config.allowed_tools
        if self._config.max_turns > 0:
            options["max_turns"] = self._config.max_turns
        if self._config.model:
            options["model"] = self._config.model
        if self._config.system_prompt_append:
            options["system_prompt_append"] = self._config.system_prompt_append
        if self._config.permission_mode:
            options["permission_mode"] = self._config.permission_mode

        run_fn = claude_code_sdk.run
        if asyncio.iscoroutinefunction(run_fn):
            raw_result = await run_fn(**options)
        else:
            loop = asyncio.get_running_loop()
            raw_result = await loop.run_in_executor(
                None, lambda: run_fn(**options),
            )

        return self._parse_sdk_result(raw_result, context)

    def _parse_sdk_result(
        self, raw_result: Any, context: BackendContext,
    ) -> BackendResult:
        if hasattr(raw_result, "model_dump"):
            data = raw_result.model_dump()
        elif isinstance(raw_result, dict):
            data = raw_result
        else:
            data = {"result": str(raw_result)}

        is_error = data.get("is_error", False)
        result_text = data.get("result", "")
        usage = data.get("usage", {})

        token_usage = self._extract_token_usage(usage)
        artifacts = self._discover_artifacts(context)
        tool_calls = self._extract_tool_calls(data)

        if is_error:
            errors = data.get("errors", [])
            error_msg = (
                "; ".join(str(e) for e in errors)
                if errors
                else "SDK execution failed"
            )
            self._raise_if_classifiable(error_msg, context)
            return BackendResult(
                status=BackendStatus.FAILED,
                error=error_msg,
                artifacts=artifacts,
                metadata={"token_usage": token_usage},
            )

        return BackendResult(
            status=BackendStatus.COMPLETED,
            summary=result_text[:SUMMARY_LIMIT],
            artifacts=artifacts,
            output=result_text,
            metadata={
                "token_usage": token_usage,
                "session_id": data.get("session_id", ""),
                "cost_usd": data.get("total_cost_usd", 0.0),
                "backend": self.BACKEND_NAME,
                "tool_calls": tool_calls,
            },
        )

    # -- CLI fallback path ---------------------------------------------------

    def _is_cli_available(self) -> bool:
        if self._cli_available is not None:
            return self._cli_available
        self._cli_available = shutil.which(self._config.cli_path) is not None
        return self._cli_available

    async def _execute_via_cli(
        self, context: BackendContext, prompt: str,
    ) -> BackendResult:
        """Execute via claude CLI subprocess."""
        cmd = self._build_cli_command(context, prompt)
        cwd = context.workspace_path or "."

        loop = asyncio.get_running_loop()
        proc = await loop.run_in_executor(
            None,
            lambda: run_with_progress(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=self._get_cli_timeout(),
            ),
        )

        if proc.timed_out:
            raise NodeTimeoutError(
                node_id=context.node.id,
                agent_type=context.node.agent_type,
                timeout=self._get_cli_timeout(),
            )

        if proc.returncode == 127:
            return BackendResult(
                status=BackendStatus.FAILED,
                error=f"Claude CLI not found at: {self._config.cli_path}",
            )

        if proc.returncode != 0:
            stderr = proc.stderr.strip()
            self._raise_if_classifiable(stderr, context)
            return BackendResult(
                status=BackendStatus.FAILED,
                error=stderr or f"claude CLI exited with code {proc.returncode}",
            )

        return self._parse_cli_output(proc.stdout, context)

    def _build_cli_command(
        self, context: BackendContext, prompt: str,
    ) -> list[str]:
        cmd = [
            self._config.cli_path,
            "-p",
            "--output-format", "json",
            "--permission-mode", self._config.permission_mode,
        ]

        if self._config.model:
            cmd.extend(["--model", self._config.model])
        if self._config.max_turns > 0:
            cmd.extend(["--max-turns", str(self._config.max_turns)])
        if self._config.max_budget_usd > 0:
            cmd.extend(["--max-budget-usd", str(self._config.max_budget_usd)])
        if self._config.system_prompt_append:
            cmd.extend(["--append-system-prompt", self._config.system_prompt_append])
        for tool in self._config.allowed_tools:
            cmd.extend(["--allowed-tools", tool])
        if context.session_id:
            cmd.extend(["--session-id", context.session_id])

        cmd.append(prompt)
        return cmd

    def _parse_cli_output(
        self, stdout: str, context: BackendContext,
    ) -> BackendResult:
        try:
            data = json.loads(stdout.strip())
        except json.JSONDecodeError as exc:
            return BackendResult(
                status=BackendStatus.FAILED,
                error=f"Failed to parse Claude CLI JSON output: {exc}",
                output=stdout[:OUTPUT_PREVIEW_LIMIT],
            )

        is_error = data.get("is_error", False)
        result_text = data.get("result", "")
        usage = data.get("usage", {})
        errors = data.get("errors", [])

        token_usage = self._extract_token_usage(usage)
        artifacts = self._discover_artifacts(context)
        tool_calls = self._extract_tool_calls(data)

        if is_error:
            error_msg = (
                "; ".join(str(e) for e in errors)
                if errors
                else "CLI execution failed"
            )
            subtype = data.get("subtype", "")
            self._raise_if_classifiable(
                error_msg, context, subtype=subtype,
            )
            return BackendResult(
                status=BackendStatus.FAILED,
                error=error_msg,
                artifacts=artifacts,
                metadata={"token_usage": token_usage},
            )

        return BackendResult(
            status=BackendStatus.COMPLETED,
            summary=result_text[:SUMMARY_LIMIT],
            artifacts=artifacts,
            output=result_text,
            metadata={
                "token_usage": token_usage,
                "session_id": data.get("session_id", ""),
                "cost_usd": data.get("total_cost_usd", 0.0),
                "backend": self.BACKEND_NAME,
                "tool_calls": tool_calls,
            },
        )

    # -- Prompt construction -------------------------------------------------

    def _build_prompt(self, context: BackendContext) -> str:
        parts: list[str] = []

        role_map = {
            "planner": (
                "You are a planning agent. Analyze the requirement"
                " and produce a detailed plan."
            ),
            "generator": (
                "You are a code generation agent."
                " Implement the described functionality."
            ),
            "evaluator": (
                "You are an evaluation agent."
                " Review and evaluate the provided code."
            ),
        }
        agent_type = context.node.agent_type
        parts.append(
            role_map.get(agent_type, "You are a helpful coding assistant.")
        )

        parts.append(f"\n## Task\n{context.node.task_description}")

        if context.artifacts:
            sections: list[str] = []
            for artifact in context.artifacts:
                section = (
                    f"### From {artifact.from_agent}"
                    f" ({artifact.metadata.get('from_node', 'unknown')})\n"
                )
                if artifact.file_paths:
                    section += f"Files: {', '.join(artifact.file_paths)}\n"
                content = artifact.content
                if len(content) > ARTIFACT_CONTENT_LIMIT:
                    content = (
                        content[:ARTIFACT_CONTENT_LIMIT]
                        + "...(truncated)"
                    )
                section += content
                sections.append(section)
            if sections:
                parts.append(f"\n## Input Context\n{''.join(sections)}")

        if context.memory_prompt:
            parts.append(f"\n{context.memory_prompt}")
        if context.project_context:
            parts.append(f"\n## Project Context\n{context.project_context}")

        return "\n".join(parts)

    # -- Artifact discovery --------------------------------------------------

    def _discover_artifacts(self, context: BackendContext) -> list[str]:
        """Discover files created/modified by Claude Code via git diff.

        Synchronous subprocess call — runs after async CLI execution
        completes, so event loop blocking is acceptable here.
        """
        workspace = context.workspace_path
        if not workspace:
            return []

        try:
            result = run_with_progress(
                ["git", "diff", "--name-only", "--diff-filter=ACMR"],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                return [
                    f.strip()
                    for f in result.stdout.strip().split("\n")
                    if f.strip()
                ]
        except OSError as exc:
            logger.debug("Artifact discovery failed: %s", exc)

        return []

    # -- Token usage extraction ----------------------------------------------

    @staticmethod
    def _extract_token_usage(
        usage: dict[str, Any] | None,
    ) -> dict[str, int]:
        """Extract token usage from SDK/CLI output.

        Returns zeroed defaults if usage is None/empty (#612 #15).
        """
        if not usage:
            return {"input_tokens": 0, "output_tokens": 0}
        return {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        }

    @staticmethod
    def _extract_tool_calls(data: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract tool call info from SDK/CLI result data (M5.1)."""
        tool_uses = data.get("tool_uses", [])
        if tool_uses:
            return [
                {
                    "name": tu.get("name", "unknown"),
                    "input_preview": str(tu.get("input", ""))[:200],
                }
                for tu in tool_uses
            ]
        messages = data.get("messages", [])
        calls = []
        for msg in messages:
            content_blocks = msg.get("content", [])
            if isinstance(content_blocks, list):
                for block in content_blocks:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        calls.append({
                            "name": block.get("name", "unknown"),
                            "input_preview": str(
                                block.get("input", "")
                            )[:200],
                        })
        return calls if calls else []

    # -- Error classification ------------------------------------------------

    def _raise_if_classifiable(
        self,
        error_message: str,
        context: BackendContext,
        subtype: str = "",
    ) -> None:
        """Classify Claude Code errors and raise if match found (#612 #6).

        Raises the appropriate Weave exception if the error is classifiable.
        Otherwise returns normally, allowing the caller to handle as generic.
        """
        error_lower = error_message.lower()

        rate_limit_patterns = (
            "rate limit", "rate_limit", "429", "too many requests",
        )
        if any(p in error_lower for p in rate_limit_patterns):
            raise RateLimitError(
                provider="claude_code",
                model=self._config.model or "default",
                retries=0,
            )

        if subtype == "error_max_budget_usd" or "budget" in error_lower:
            raise BudgetExhaustedError(
                used_tokens=0,
                budget_tokens=0,
                node_id=context.node.id,
            )

        timeout_patterns = ("timeout", "timed out", "deadline exceeded")
        if any(p in error_lower for p in timeout_patterns):
            raise NodeTimeoutError(
                node_id=context.node.id,
                agent_type=context.node.agent_type,
                timeout=0,
            )

    # Backward-compat alias for callers/tests that use the old name.
    def _classify_error(
        self,
        error_message: str,
        context: BackendContext,
        subtype: str = "",
    ) -> Exception | None:
        """Return classified exception or None (backward-compat wrapper)."""
        try:
            self._raise_if_classifiable(error_message, context, subtype)
        except (RateLimitError, NodeTimeoutError, BudgetExhaustedError) as exc:
            return exc
        return None

    # -- Helpers --------------------------------------------------------------

    def _get_cli_timeout(self) -> int:
        if self._config.timeout_override > 0:
            return self._config.timeout_override
        return DEFAULT_CLI_TIMEOUT
