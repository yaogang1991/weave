"""CodexBackend -- executes DAG nodes via OpenAI Codex CLI."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from core.backend_models import BackendContext, BackendResult, BackendStatus
from core.config import CodexBackendConfig
from core.exceptions import BudgetExhaustedError, NodeTimeoutError, RateLimitError
from core.subprocess_runner import run_with_progress
from agent.backends.base import AgentBackend
from monitoring.otel import inject_trace_context, start_backend_call_span

logger = logging.getLogger(__name__)

# Named constants (#619).
ARTIFACT_CONTENT_LIMIT = 2000
SUMMARY_LIMIT = 200

__all__ = ["CodexBackend"]


class CodexBackend(AgentBackend):
    """Wraps the Codex CLI (`codex exec --json`) as an AgentBackend.

    Codex manages its own sandbox, tools, and LLM connection.
    """

    def __init__(self, config: CodexBackendConfig | None = None) -> None:
        cfg = config or CodexBackendConfig()
        self._binary_path = cfg.binary_path
        self._model = cfg.model
        self._sandbox_mode = cfg.sandbox_mode
        self._timeout = cfg.timeout
        self._mcp_config = cfg.mcp_config
        self._resolved_path: str | None = None

    @property
    def name(self) -> str:
        return "codex"

    async def health_check(self) -> bool:
        # Re-resolve each time (#619 #8 — no permanent cache).
        self._resolved_path = shutil.which(self._binary_path)
        return self._resolved_path is not None

    def get_capabilities(self) -> list[str]:
        return ["generator"]

    async def execute(self, context: BackendContext) -> BackendResult:
        if self._resolved_path is None:
            return BackendResult(
                status=BackendStatus.FAILED,
                error="codex binary not resolved -- health_check() not called or failed",
            )

        prompt = self._build_prompt(context)
        cwd = context.workspace_path or "."
        sandbox = self._sandbox_mode

        with start_backend_call_span(
            context.run_id or "", context.node.id, self.name,
        ):
            env = inject_trace_context(dict(os.environ))

            # Write MCP config for codex subprocess (M6.8).
            mcp_config_path: Path | None = None
            if self._mcp_config:
                mcp_config_path = self._write_codex_mcp_config(
                    self._mcp_config, cwd,
                )

            try:
                cmd = [
                    self._resolved_path, "exec", "--json",
                    f"--sandbox={sandbox}",
                    f"--model={self._model}",
                ]
                if mcp_config_path:
                    cmd.extend(["--mcp-config", str(mcp_config_path)])
                cmd.extend(["--", prompt])

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
                    error=f"codex binary not found: {self._resolved_path}",
                )

            # Mutable containers for streaming output (#619 #9 — documented).
            output_lines: list[str] = []
            usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

            try:
                await asyncio.wait_for(
                    self._stream_output(
                        process, output_lines, usage,
                        context.cancel_event, context.progress_callback,
                    ),
                    timeout=self._timeout,
                )
            except asyncio.TimeoutError:
                if process.returncode is None:
                    process.kill()
                raise NodeTimeoutError(
                    node_id=context.node.id,
                    agent_type=context.node.agent_type,
                    timeout=self._timeout,
                )
            finally:
                from mcp.config_export import MCPConfigExporter
                MCPConfigExporter.cleanup_config(mcp_config_path)

            stderr = ""
            if process.stderr is not None:
                stderr_bytes = await process.stderr.read()
                stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

            # Error classification (#619 #5).
            self._raise_if_classifiable(stderr, context)

            # Artifact discovery (#619 #2).
            artifacts = self._discover_artifacts(context)

            return self._parse_result(
                process.returncode, output_lines, usage, stderr, artifacts,
            )

    def _build_prompt(self, context: BackendContext) -> str:
        parts: list[str] = []
        # Typed field access (#619 #7).
        parts.append(context.node.task_description)

        for art in context.artifacts:
            content = art.content
            if content:
                # Truncation with indicator (#619 #6).
                if len(content) > ARTIFACT_CONTENT_LIMIT:
                    content = content[:ARTIFACT_CONTENT_LIMIT] + "...(truncated)"
                parts.append(f"\n=== PREVIOUS OUTPUT ===\n{content}")
            if art.file_paths:
                parts.append(
                    f"\n=== RELEVANT FILES ===\n{', '.join(art.file_paths)}"
                )

        if context.memory_prompt:
            parts.append(f"\n{context.memory_prompt}")
        if context.project_context:
            parts.append(f"\n=== PROJECT CONTEXT ===\n{context.project_context}")

        return "\n".join(parts)

    async def _stream_output(
        self,
        process: asyncio.subprocess.Process,
        output_lines: list[str],
        usage: dict[str, int],
        cancel_event: Any | None,
        progress_callback: Any | None,
    ) -> None:
        """Stream JSONL output from Codex process.

        Note: mutates output_lines and usage in-place for streaming
        performance (#619 #9).
        """
        if process.stdout is None:
            return

        while True:
            if cancel_event is not None and cancel_event.is_set():
                process.terminate()
                # Follow up with kill if terminate doesn't work (#619 #16).
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                return

            line_bytes = await process.stdout.readline()
            if not line_bytes:
                break

            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("Codex: skipping non-JSON line: %s", line[:100])
                continue

            event_type = event.get("type", "")

            if event_type == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    text = item.get("text", "")
                    if text:
                        output_lines.append(text)
                if progress_callback is not None:
                    try:
                        progress_callback()
                    except Exception:
                        logger.debug("progress_callback raised, ignoring", exc_info=True)

            elif event_type == "turn.completed":
                turn_usage = event.get("usage", {})
                usage["input_tokens"] += turn_usage.get("input_tokens", 0)
                usage["output_tokens"] += turn_usage.get("output_tokens", 0)

    def _parse_result(
        self,
        returncode: int | None,
        output_lines: list[str],
        usage: dict[str, int],
        stderr: str,
        artifacts: list[str],
    ) -> BackendResult:
        if returncode is None or returncode < 0:
            return BackendResult(
                status=BackendStatus.CANCELLED,
                output="\n".join(output_lines),
                artifacts=artifacts,
                metadata={"token_usage": usage},
            )

        if returncode != 0:
            return BackendResult(
                status=BackendStatus.FAILED,
                error=stderr or f"codex exited with code {returncode}",
                output="\n".join(output_lines),
                artifacts=artifacts,
                metadata={"token_usage": usage},
            )

        output = "\n".join(output_lines)
        # Use first line as summary, not last (#619 #10).
        summary = output_lines[0][:SUMMARY_LIMIT] if output_lines else ""

        return BackendResult(
            status=BackendStatus.COMPLETED,
            summary=summary,
            artifacts=artifacts,
            output=output,
            metadata={
                "token_usage": usage,
                "backend": "codex",
            },
        )

    def _discover_artifacts(self, context: BackendContext) -> list[str]:
        """Discover files created/modified by Codex via git diff (#619 #2)."""
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
            logger.debug("Codex artifact discovery failed: %s", exc)

        return []

    def _raise_if_classifiable(
        self,
        error_message: str,
        context: BackendContext,
    ) -> None:
        """Classify Codex errors and raise if match found (#619 #5)."""
        if not error_message:
            return

        error_lower = error_message.lower()

        rate_limit_patterns = (
            "rate limit", "rate_limit", "429", "too many requests",
        )
        if any(p in error_lower for p in rate_limit_patterns):
            raise RateLimitError(
                provider="codex",
                model=self._model,
                retries=0,
            )

        if "budget" in error_lower:
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
                timeout=self._timeout,
            )

    @staticmethod
    def _write_codex_mcp_config(
        mcp_config: Any, cwd: str,
    ) -> Path | None:
        """Write MCP config in .mcp.json format for Codex subprocess (M6.8).

        Codex CLI accepts the same --mcp-config flag as Claude Code.
        Falls back to writing .mcp.json if MCPConfigExporter is available.
        """
        try:
            from mcp.config_export import MCPConfigExporter
            return MCPConfigExporter.write_config(mcp_config, cwd, suffix="codex")
        except Exception:
            logger.debug("MCP config export for Codex failed", exc_info=True)
            return None
