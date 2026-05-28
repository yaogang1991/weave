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
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from core.backend_models import BackendContext, BackendResult, BackendStatus
from core.exceptions import BudgetExhaustedError, NodeTimeoutError, RateLimitError
from core.subprocess_runner import run_with_progress
from agent.backends.base import AgentBackend
from agent.backends.stderr_tail import StderrTail
from agent.backends.stream_parser import StreamParser
from core.activity_detector import is_meaningful_event
from monitoring.otel import inject_trace_context, start_backend_call_span

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
        "_timeout_override", "_mcp_config",
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
        mcp_config: Any = None,
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
        self._mcp_config = mcp_config

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
            mcp_config=getattr(config, 'mcp_config', None),
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

    @property
    def mcp_config(self) -> Any:
        return self._mcp_config


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

        messages: list[dict[str, Any]] = []
        if result_text:
            messages.append({"raw_type": "result", "data": {"result": result_text}})

        sdk_session_id = data.get("session_id", "")

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
                messages=messages,
                session_id=sdk_session_id,
                can_resume=bool(sdk_session_id),
            )

        return BackendResult(
            status=BackendStatus.COMPLETED,
            summary=result_text[:SUMMARY_LIMIT],
            artifacts=artifacts,
            output=result_text,
            metadata={
                "token_usage": token_usage,
                "session_id": sdk_session_id,
                "cost_usd": data.get("total_cost_usd", 0.0),
                "backend": self.BACKEND_NAME,
                "tool_calls": tool_calls,
            },
            messages=messages,
            session_id=sdk_session_id,
            can_resume=bool(sdk_session_id),
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
        """Execute via claude CLI subprocess with stream-json output."""
        cwd = context.workspace_path or "."

        with start_backend_call_span(
            context.run_id or "", context.node.id, self.BACKEND_NAME,
        ):
            env = inject_trace_context(dict(os.environ))

            # Write MCP config for --mcp-config support (M6.8).
            mcp_config_path: Path | None = None
            if self._config.mcp_config:
                from mcp.config_export import MCPConfigExporter
                mcp_config_path = MCPConfigExporter.write_config(
                    self._config.mcp_config, cwd,
                )

            cmd = self._build_cli_command(context, prompt, mcp_config_path)

            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                )
            except FileNotFoundError:
                return BackendResult(
                    status=BackendStatus.FAILED,
                    error=f"Claude CLI not found at: {self._config.cli_path}",
                )

            parser = StreamParser()
            stderr_tail = StderrTail()
            usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
            # Mutable containers for streaming output.
            state: dict[str, str] = {"session_id": "", "result": "", "error": ""}

            try:
                await asyncio.wait_for(
                    self._stream_cli_output(
                        process, parser, usage, state,
                        context.cancel_event, context.progress_callback,
                        context.event_callback, context.activity_detector,
                        stderr_tail,
                    ),
                    timeout=self._get_cli_timeout(),
                )
            except asyncio.TimeoutError:
                if process.returncode is None:
                    process.kill()
                raise NodeTimeoutError(
                    node_id=context.node.id,
                    agent_type=context.node.agent_type,
                    timeout=self._get_cli_timeout(),
                )
            finally:
                # Cleanup MCP config file after execution (M6.8).
                if mcp_config_path:
                    from mcp.config_export import MCPConfigExporter
                    MCPConfigExporter.cleanup_config(mcp_config_path)

            if process.stderr is not None:
                stderr_bytes = await process.stderr.read()
                stderr_text = stderr_bytes.decode("utf-8", errors="replace")
                for line in stderr_text.splitlines():
                    stderr_tail.write(line + "\n")

            stderr = stderr_tail.tail().strip()

            if process.returncode == 127:
                return BackendResult(
                    status=BackendStatus.FAILED,
                    error=f"Claude CLI not found at: {self._config.cli_path}",
                )

            if process.returncode is not None and process.returncode != 0:
                self._raise_if_classifiable(stderr, context)
                error_detail = stderr or f"claude CLI exited with code {process.returncode}"
                return BackendResult(
                    status=BackendStatus.FAILED,
                    error=error_detail,
                    messages=[m.model_dump() for m in parser.messages],
                )

            artifacts = self._discover_artifacts(context)
            return self._build_stream_result(
                parser, usage, state, artifacts,
            )

    async def _stream_cli_output(
        self,
        process: asyncio.subprocess.Process,
        parser: StreamParser,
        usage: dict[str, int],
        state: dict[str, str],
        cancel_event: Any | None,
        progress_callback: Any | None,
        event_callback: Any | None,
        activity_detector: Any | None = None,
        stderr_tail: StderrTail | None = None,
    ) -> None:
        """Stream NDJSON output from Claude CLI process."""
        if process.stdout is None:
            return

        # Spawn stderr reader task to capture stderr in real-time.
        stderr_task: asyncio.Task | None = None
        if process.stderr is not None and stderr_tail is not None:
            async def _read_stderr() -> None:
                while True:
                    line_bytes = await process.stderr.readline()
                    if not line_bytes:
                        break
                    stderr_tail.write(
                        line_bytes.decode("utf-8", errors="replace"),
                    )
            stderr_task = asyncio.create_task(_read_stderr())

        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        process.kill()
                    return

                line_bytes = await process.stdout.readline()
                if not line_bytes:
                    break

                line = line_bytes.decode("utf-8", errors="replace").strip()
                msg = parser.feed_line(line)
                if msg is None:
                    continue

                # M6.6: Record meaningful stream events for semantic timeout.
                if activity_detector is not None and is_meaningful_event(msg.raw_type):
                    activity_detector.record_activity(msg.raw_type)

                # Notify event_callback for every valid message.
                if event_callback is not None:
                    try:
                        event_callback(msg.raw_type, msg.data)
                    except Exception:
                        logger.debug("event_callback raised, ignoring", exc_info=True)

                # Notify progress_callback for every valid message.
                if progress_callback is not None:
                    try:
                        progress_callback()
                    except Exception:
                        logger.debug("progress_callback raised, ignoring", exc_info=True)

                if msg.raw_type == "assistant":
                    # Accumulate usage from intermediate assistant events.
                    msg_usage = msg.data.get("message", {}).get("usage", {}) or {}
                    usage["input_tokens"] += msg_usage.get("input_tokens") or 0
                    usage["output_tokens"] += msg_usage.get("output_tokens") or 0
                    # Extract tool_use content blocks and emit as separate events.
                    if event_callback is not None:
                        content_blocks = (
                            msg.data.get("message", {}).get("content", [])
                        )
                        if isinstance(content_blocks, list):
                            for block in content_blocks:
                                if (
                                    isinstance(block, dict)
                                    and block.get("type") == "tool_use"
                                ):
                                    try:
                                        event_callback("tool_use", block)
                                    except Exception:
                                        logger.debug(
                                            "tool_use event_callback raised",
                                            exc_info=True,
                                        )
                    # Capture session_id from assistant events.
                    sid = msg.data.get("message", {}).get("session_id", "")
                    if sid:
                        state["session_id"] = sid

                elif msg.raw_type == "result":
                    # Result event provides final output + usage (overrides).
                    result_text = msg.data.get("result", "")
                    if result_text:
                        state["result"] = result_text
                    result_usage = msg.data.get("usage", {}) or {}
                    if result_usage:
                        usage["input_tokens"] = (
                            result_usage.get("input_tokens") or 0
                        )
                        usage["output_tokens"] = (
                            result_usage.get("output_tokens") or 0
                        )
                    sid = msg.data.get("session_id", "")
                    if sid:
                        state["session_id"] = sid
                    if msg.data.get("is_error"):
                        state["error"] = result_text

                elif msg.raw_type == "user":
                    # Extract tool_result content blocks and emit as events.
                    if event_callback is not None:
                        content_blocks = (
                            msg.data.get("message", {}).get("content", [])
                        )
                        if isinstance(content_blocks, list):
                            for block in content_blocks:
                                if (
                                    isinstance(block, dict)
                                    and block.get("type") == "tool_result"
                                ):
                                    try:
                                        event_callback("tool_result", block)
                                    except Exception:
                                        logger.debug(
                                            "tool_result event_callback raised",
                                            exc_info=True,
                                        )
                    sid = msg.data.get("session_id", "")
                    if sid:
                        state["session_id"] = sid
        finally:
            if stderr_task is not None:
                stderr_task.cancel()
                try:
                    await stderr_task
                except (asyncio.CancelledError, Exception):
                    pass

    def _build_stream_result(
        self,
        parser: StreamParser,
        usage: dict[str, int],
        state: dict[str, str],
        artifacts: list[str],
    ) -> BackendResult:
        """Build BackendResult from accumulated stream state."""
        result_text = state.get("result", "")
        session_id = state.get("session_id", "")
        error_text = state.get("error", "")

        if error_text:
            return BackendResult(
                status=BackendStatus.FAILED,
                error=error_text,
                artifacts=artifacts,
                metadata={"token_usage": usage, "session_id": session_id},
                messages=[m.model_dump() for m in parser.messages],
                session_id=session_id,
                can_resume=bool(session_id),
            )

        return BackendResult(
            status=BackendStatus.COMPLETED,
            summary=result_text[:SUMMARY_LIMIT],
            artifacts=artifacts,
            output=result_text,
            metadata={
                "token_usage": usage,
                "session_id": session_id,
                "backend": self.BACKEND_NAME,
            },
            messages=[m.model_dump() for m in parser.messages],
            session_id=session_id,
            can_resume=bool(session_id),
        )

    def _build_cli_command(
        self, context: BackendContext, prompt: str,
        mcp_config_path: str | None = None,
    ) -> list[str]:
        cmd = [
            self._config.cli_path,
            "-p",
            "--output-format", "stream-json",
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
        # M6.7: Resume previous session when resume_session_id is provided.
        if context.resume_session_id:
            cmd.append("--resume")
            if "--session-id" not in cmd:
                cmd.extend(["--session-id", context.resume_session_id])
        if mcp_config_path:
            cmd.extend(["--mcp-config", str(mcp_config_path)])

        cmd.append(prompt)
        return cmd

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
