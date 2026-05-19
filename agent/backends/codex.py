"""CodexBackend -- executes DAG nodes via OpenAI Codex CLI."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any

from core.backend_models import BackendContext, BackendResult, BackendStatus
from core.config import CodexBackendConfig
from agent.backends.base import AgentBackend

logger = logging.getLogger(__name__)

_VALID_SANDBOX_MODES = frozenset({"workspace-write", "workspace-read", "full-access", "none", "readOnly", "dangerFullAccess"})


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
        self._resolved_path: str | None = None

    @property
    def name(self) -> str:
        return "codex"

    async def health_check(self) -> bool:
        if self._resolved_path is None:
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
        sandbox = self._sandbox_mode if self._sandbox_mode in _VALID_SANDBOX_MODES else "workspace-write"

        try:
            process = await asyncio.create_subprocess_exec(
                self._resolved_path, "exec", "--json",
                f"--sandbox={sandbox}",
                f"--model={self._model}",
                "--",
                prompt,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
        except FileNotFoundError:
            return BackendResult(
                status=BackendStatus.FAILED,
                error=f"codex binary not found: {self._resolved_path}",
            )

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
            return BackendResult(
                status=BackendStatus.FAILED,
                error=f"codex timed out after {self._timeout}s",
            )
        except Exception as exc:
            if process.returncode is None:
                process.kill()
            return BackendResult(
                status=BackendStatus.FAILED, error=str(exc),
            )

        stderr = ""
        if process.stderr is not None:
            stderr_bytes = await process.stderr.read()
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

        return self._parse_result(process.returncode, output_lines, usage, stderr)

    def _build_prompt(self, context: BackendContext) -> str:
        parts: list[str] = []
        task = getattr(context.node, "task_description", "") or ""
        parts.append(task)

        for art in context.artifacts:
            content = getattr(art, "content", None)
            paths = getattr(art, "file_paths", None)
            if content:
                parts.append(f"\n=== PREVIOUS OUTPUT ===\n{content}")
            if paths:
                parts.append(f"\n=== RELEVANT FILES ===\n{', '.join(paths)}")

        return "\n".join(parts)

    async def _stream_output(
        self,
        process: asyncio.subprocess.Process,
        output_lines: list[str],
        usage: dict[str, int],
        cancel_event: Any | None,
        progress_callback: Any | None,
    ) -> None:
        if process.stdout is None:
            return

        while True:
            if cancel_event is not None and cancel_event.is_set():
                process.terminate()
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
    ) -> BackendResult:
        if returncode is None or returncode < 0:
            return BackendResult(
                status=BackendStatus.CANCELLED,
                output="\n".join(output_lines),
                metadata={"token_usage": usage},
            )

        if returncode != 0:
            return BackendResult(
                status=BackendStatus.FAILED,
                error=stderr or f"codex exited with code {returncode}",
                output="\n".join(output_lines),
                metadata={"token_usage": usage},
            )

        output = "\n".join(output_lines)
        summary = output_lines[-1][:200] if output_lines else ""

        return BackendResult(
            status=BackendStatus.COMPLETED,
            summary=summary,
            output=output,
            metadata={"token_usage": usage},
        )
