"""
Agent Worker: the "dumb loop" that calls the LLM and executes tools.
All intelligence lives in the model. Weave just orchestrates.

Enhanced with:
- Context window management (token estimation + message truncation)
- API retry with exponential backoff for transient errors
- Artifact tracking (files created/modified via write/edit tools)
- Auto-retry for empty tool call args (#282)
- Circuit breaker for consecutive empty iterations (#290)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, TYPE_CHECKING

import json
import logging
import threading
import uuid

from core.models import AgentMessage, ToolCall, EventType
from core.config import LLMConfig
from core.llm_client import LLMClient
from core.context import ContextManager
from core.stuck_detector import StuckDetector
from session.store import SessionStore

if TYPE_CHECKING:
    from memory.manager import MemoryManager

logger = logging.getLogger(__name__)

# Required arguments for known tools — used to validate LLM tool calls
# before execution, catching empty/malformed arguments from some models (#215).
TOOL_REQUIRED_ARGS: dict[str, list[str]] = {
    "write": ["file_path", "content"],
    "edit": ["file_path", "old_string", "new_string"],
    "read": ["file_path"],
    "bash": ["command"],
    "glob": ["pattern"],
    "grep": ["pattern"],
}

# Fields that must not be empty strings even when present and non-None.
# Tools like write/edit legitimately use content="" or new_string="" (e.g. clearing
# a file), but bash.command, read.file_path, and grep.pattern are meaningless when blank.
TOOL_NON_EMPTY_ARGS: dict[str, list[str]] = {
    "bash": ["command"],
    "read": ["file_path"],
    "grep": ["pattern"],
    "glob": ["pattern"],
}

# Circuit breaker: consecutive iterations where ALL tool calls have missing/blank
# args trigger a forced exit to prevent infinite loops (#290).
EMPTY_TOOL_CALL_LIMIT = 10

# Degenerate loop breaker: consecutive iterations where ALL tool calls have
# completely empty args {} — the LLM is stuck and will not recover (#345).
# Lower threshold because empty-dict args are unrecoverable, while missing-one-
# field args might self-correct.  At ~55s per LLM call, 3 iters = ~3 min wasted.
DEGENERATE_CALL_LIMIT = 3

# Auto-retry: when ALL tool calls in a single LLM response have missing/blank
# args, re-request the LLM before advancing to the next iteration (#282).
# Prevents cascading failures in parallel execution where some models produce
# empty args under concurrent load.
EMPTY_CALL_MAX_RETRIES = 3


class AgentWorker:
    """
    Minimal Weave loop:
    while has_tool_calls:
        call LLM with messages
        execute tool calls
        feed results back
    """

    def __init__(
        self,
        config: LLMConfig,
        session_store: SessionStore,
        max_context_tokens: int = 100_000,
        base_cwd: str | None = None,
        memory_manager: MemoryManager | None = None,
        output_monitor: Any | None = None,
    ):
        self.config = config
        self.session_store = session_store
        self.llm = LLMClient(config)
        self.max_context_tokens = max_context_tokens
        self.artifacts: list[str] = []
        self._base_cwd = Path(base_cwd).resolve() if base_cwd else None
        self._memory_manager = memory_manager
        self._output_monitor = output_monitor
        self._context_manager = ContextManager(max_tokens=max_context_tokens)
        self._run_token_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self.last_token_usage: dict[str, int] = {}

    # -- Public interface ---------------------------------------------------

    def run(
        self,
        session_id: str,
        system_prompt: str,
        user_message: str,
        tools: list[dict],
        tool_executor,
        max_iterations: int = 50,
        cancel_event: threading.Event | None = None,
        progress_callback: Any | None = None,
    ) -> Iterator[AgentMessage]:
        """
        Run the agent loop until no more tool calls or max iterations reached.
        Yields each assistant message for streaming/real-time observation.

        Args:
            cancel_event: Cooperative cancellation — if set, the loop exits
                at the next iteration boundary (#360 PR2).
            progress_callback: Called after each LLM response and tool
                execution to report progress to the watchdog (#360 PR3).
        """
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        # Inject memory context if memory manager is available (#481)
        if self._memory_manager and self._memory_manager.config.enabled:
            memory_entries = self._memory_manager.get_context_for_agent(
                agent_type="shared",
                task_description=user_message,
                session_id=session_id,
            )
            if memory_entries:
                memory_prompt = self._memory_manager.format_memory_prompt(
                    memory_entries,
                )
                if memory_prompt:
                    messages.append({
                        "role": "user",
                        "content": memory_prompt,
                    })

        self.artifacts = []
        self._run_token_usage = {"input_tokens": 0, "output_tokens": 0}
        stuck_detector = StuckDetector(
            empty_call_limit=EMPTY_TOOL_CALL_LIMIT,
            degenerate_call_limit=DEGENERATE_CALL_LIMIT,
        )

        for iteration in range(max_iterations):
            # Cooperative cancellation check (#360 PR2)
            if cancel_event is not None and cancel_event.is_set():
                logger.info(
                    "Agent loop cancelled at iteration %d/%d (cooperative)",
                    iteration, max_iterations,
                )
                break

            # Context management: compact if exceeding threshold (#480)
            if self._context_manager.should_compact(messages):
                messages = self._context_manager.compact(messages, self.llm)
            else:
                messages = self._truncate_messages(messages, self.max_context_tokens)

            # Call LLM with auto-retry for empty tool call args (#282).
            # When ALL tool calls have missing/blank args, re-request the LLM
            # (up to EMPTY_CALL_MAX_RETRIES) before counting as an empty iteration.
            tool_results: list[dict] = []
            any_tool_executed = False
            assistant_message: dict = {}

            for llm_attempt in range(EMPTY_CALL_MAX_RETRIES + 1):
                assistant_message = self.llm.call(messages, tools)

                # M4.2: Track token usage from LLM response
                usage = assistant_message.get("usage", {})
                self._run_token_usage["input_tokens"] += usage.get("input_tokens", 0)
                self._run_token_usage["output_tokens"] += usage.get("output_tokens", 0)

                # Report progress: LLM responded (#360 PR3)
                if progress_callback:
                    progress_callback()

                self.session_store.emit_event(
                    session_id,
                    EventType.AGENT_MESSAGE,
                    assistant_message,
                )

                if (
                    "tool_calls" not in assistant_message
                    or not assistant_message["tool_calls"]
                ):
                    yield AgentMessage(
                        role="assistant",
                        content=assistant_message.get("content", ""),
                    )
                    # Persist memory before exiting (#481)
                    self._persist_memory(session_id, user_message, messages)
                    return  # No tool calls → done

                tool_results, any_tool_executed = self._execute_tool_calls(
                    assistant_message, session_id, tool_executor,
                    progress_callback=progress_callback,
                )

                if any_tool_executed:
                    break  # At least one valid tool call → proceed normally

                # Early termination for completely empty args (#541).
                # When ALL tool calls have args={}, retrying is futile — the
                # LLM produced no arguments at all and error feedback won't help.
                # Skip remaining retries and go straight to degenerate detection.
                all_empty_args = all(
                    tc.get("arguments") == {}
                    for tc in assistant_message.get("tool_calls", [])
                )
                if all_empty_args:
                    logger.warning(
                        "All tool calls have empty args {} — skipping "
                        "remaining retries, entering degenerate detection (#541)",
                    )
                    break

                # All tool calls were invalid — retry LLM if attempts remain (#282)
                if llm_attempt < EMPTY_CALL_MAX_RETRIES:
                    # Log raw tool calls for debugging model-specific issues (#334)
                    raw_calls = [
                        {"name": tc.get("name"), "args": tc.get("arguments", {})}
                        for tc in assistant_message.get("tool_calls", [])
                    ]
                    logger.warning(
                        "All tool calls invalid (LLM retry %d/%d), re-requesting (#282). "
                        "Raw calls: %s",
                        llm_attempt + 1, EMPTY_CALL_MAX_RETRIES,
                        json.dumps(raw_calls)[:500],
                    )
                    # Feed back error results so LLM can correct itself
                    messages.append(assistant_message)
                    messages.extend(tool_results)
                    tool_results = []
                # else: exhausted retries → fall through to circuit breaker

            # Yield the final assistant message (after any retries)
            yield AgentMessage(
                role="assistant",
                content=assistant_message.get("content", ""),
                tool_calls=[ToolCall(**tc) for tc in assistant_message["tool_calls"]],
            )

            messages.append(assistant_message)
            messages.extend(tool_results)

            # M4.2: Stuck detection via composable StuckDetector
            stuck_result = stuck_detector.observe(assistant_message, any_tool_executed)

            # P1 (#607): Inject recovery hint on first degenerate detection
            if stuck_result.needs_hint:
                logger.warning(
                    "Degenerate empty-args detected — injecting recovery hint (#607)",
                )
                self.session_store.emit_event(
                    session_id,
                    EventType.AGENT_STUCK,
                    {
                        "pattern": "degenerate_args_recovery",
                        "consecutive_count": stuck_result.consecutive_count,
                        "message": "Injecting recovery hint for empty-args loop",
                    },
                )
                messages.append({
                    "role": "assistant",
                    "content": "I need to call a tool to continue.",
                })
                # #625: Include known workspace files in recovery hint
                # to help test nodes understand the generated codebase.
                recovery_hint = (
                    "CRITICAL: Your previous tool call had empty arguments {}. "
                    "You MUST provide complete arguments in the correct format. "
                    "For example, when calling 'write', include both 'file_path' "
                    "and 'content'. When calling 'edit', include 'file_path', "
                    "'old_string', and 'new_string'. Do NOT repeat the empty call."
                )
                if self.artifacts:
                    file_list = "\n".join(
                        f"  - {f}" for f in self.artifacts[:50]
                    )
                    recovery_hint += (
                        f"\n\nKnown workspace files:\n{file_list}\n"
                        "Use the READ tool to inspect these files before "
                        "writing tests or making changes."
                    )
                messages.append({
                    "role": "user",
                    "content": recovery_hint,
                })
                continue  # Give model another chance without counting toward limit

            if stuck_result.is_stuck:
                logger.error(
                    "Stuck detector triggered: %s (pattern=%s, count=%d/%d)",
                    stuck_result.message,
                    stuck_result.pattern.value if stuck_result.pattern else "unknown",
                    stuck_result.consecutive_count,
                    stuck_result.threshold,
                )
                self.session_store.emit_event(
                    session_id,
                    EventType.AGENT_STUCK,
                    {
                        "pattern": stuck_result.pattern.value if stuck_result.pattern else None,
                        "consecutive_count": stuck_result.consecutive_count,
                        "threshold": stuck_result.threshold,
                        "message": stuck_result.message,
                    },
                )
                break

        # Extract and store key learnings to memory (#481)
        self._persist_memory(session_id, user_message, messages)

        # M4.2: Persist token usage for this run
        self.last_token_usage = dict(self._run_token_usage)

    def _execute_tool_calls(
        self,
        assistant_message: dict,
        session_id: str,
        tool_executor,
        progress_callback: Any | None = None,
    ) -> tuple[list[dict], bool]:
        """Validate and execute tool calls from an LLM response.

        Returns (tool_results, any_tool_executed).
        """
        tool_results: list[dict] = []
        any_tool_executed = False

        for tc in assistant_message["tool_calls"]:
            # Defensive: ensure tool_call_id is present (#169)
            tool_call_id = tc.get("id") or ""
            if not tool_call_id.strip():
                tool_call_id = f"tool_{uuid.uuid4().hex[:8]}"
                tc["id"] = tool_call_id

            self.session_store.emit_event(
                session_id,
                EventType.AGENT_TOOL_USE,
                tc,
            )

            # Validate required arguments before execution (#215)
            tool_name = tc["name"]
            args = tc.get("arguments", {})
            required = TOOL_REQUIRED_ARGS.get(tool_name, [])
            missing = [k for k in required if k not in args or args[k] is None]

            if missing:
                error_content = (
                    f"Error: '{tool_name}' tool is missing required argument(s): "
                    f"{', '.join(missing)}. "
                    f"Your call had arguments: {json.dumps(args)}. "
                    f"Please retry with all required arguments."
                )
                self._append_invalid_tool_result(
                    session_id, tool_call_id, tool_name, error_content, tool_results,
                )
                logger.warning(
                    "Tool %s called with missing args: %s (raw args: %s, call_id: %s)",
                    tool_name, missing, json.dumps(args)[:200], tool_call_id,
                )
                continue

            # Tool-specific empty-string validation (#215).
            non_empty = TOOL_NON_EMPTY_ARGS.get(tool_name, [])
            blank = [k for k in non_empty if isinstance(args.get(k), str) and args[k].strip() == ""]
            if blank:
                error_content = (
                    f"Error: '{tool_name}' tool argument(s) "
                    f"{', '.join(blank)} must not be empty/blank. "
                    f"Please retry with non-empty values."
                )
                self._append_invalid_tool_result(
                    session_id, tool_call_id, tool_name, error_content, tool_results,
                )
                logger.warning(
                    "Tool %s called with blank args: %s (raw args: %s, call_id: %s)",
                    tool_name, blank, json.dumps(args)[:200], tool_call_id,
                )
                continue

            result = tool_executor.execute(tool_name, args)
            any_tool_executed = True

            # Report progress: tool executed (#360 PR3)
            if progress_callback:
                progress_callback()

            # Output monitoring: scan tool results for injection (#511 output layer)
            result_content = (
                result.output if result.success else f"Error: {result.error}"
            )
            if self._output_monitor is not None and result.success:
                scan = self._output_monitor.scan_tool_output(
                    tool_name, args, result.output,
                )
                if scan.injected:
                    result_content = scan.sanitized_output
                    self.session_store.emit_event(
                        session_id,
                        EventType.AGENT_ERROR,
                        {
                            "error": "output_injection_detected",
                            "tool": tool_name,
                            "risk_level": scan.risk_level,
                            "patterns": scan.patterns_matched,
                        },
                    )

            tool_results.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": result_content,
            })

            # Track artifacts from successful write/edit calls
            if result.success:
                self._track_artifact(tc["name"], tc.get("arguments", {}))

            self.session_store.emit_event(
                session_id,
                EventType.AGENT_TOOL_RESULT,
                {
                    "tool_call_id": tool_call_id,
                    "success": result.success,
                    "output": result.output,
                    "error": result.error,
                    "duration_ms": result.duration_ms,
                },
            )

        return tool_results, any_tool_executed

    def _append_invalid_tool_result(
        self,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
        error_content: str,
        tool_results: list[dict],
    ) -> None:
        """Append a tool result error and emit AGENT_TOOL_RESULT event.

        Ensures the session trace stays complete — every AGENT_TOOL_USE must be
        paired with an AGENT_TOOL_RESULT, even for validation failures.
        """
        tool_results.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": error_content,
        })
        self.session_store.emit_event(
            session_id,
            EventType.AGENT_TOOL_RESULT,
            {
                "tool": tool_name,
                "success": False,
                "error": error_content,
                "tool_call_id": tool_call_id,
            },
        )

    # -- Context window management ------------------------------------------

    @staticmethod
    def _estimate_tokens(messages: list[dict]) -> int:
        """Estimate token count with CJK-aware character counting (#479).

        CJK characters are ~1.5-2 chars/token; English/code is ~3.5-4 chars/token.
        """
        total_tokens = 0
        for m in messages:
            content = str(m.get("content", ""))
            cjk = sum(1 for c in content if '\u4e00' <= c <= '\u9fff')
            other = len(content) - cjk
            total_tokens += cjk // 2 + other // 4
            for tc in m.get("tool_calls", []):
                arg_str = str(tc.get("arguments", {}))
                cjk_a = sum(1 for c in arg_str if '\u4e00' <= c <= '\u9fff')
                other_a = len(arg_str) - cjk_a
                total_tokens += cjk_a // 2 + other_a // 4
        return max(total_tokens, 1)

    def _truncate_messages(
        self, messages: list[dict], max_tokens: int
    ) -> list[dict]:
        """Truncate oldest messages, keeping system prompt + last N exchanges."""
        if self._estimate_tokens(messages) <= max_tokens:
            return messages

        system = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        # Keep last 20 messages (roughly 10 tool exchanges)
        keep_tail = 20
        if len(non_system) > keep_tail:
            non_system = non_system[-keep_tail:]

        return system + non_system

    # -- Artifact tracking --------------------------------------------------

    def _track_artifact(self, tool_name: str, arguments: dict) -> None:
        """Track file paths from successful write/edit tool calls.

        Verifies the file actually exists on disk before recording,
        preventing false-positive artifact claims (#158).
        """
        if tool_name in ("write", "edit") and "file_path" in arguments:
            path = arguments["file_path"]
            try:
                p = Path(path)
                if not p.is_absolute() and self._base_cwd:
                    p = self._base_cwd / p
                if not p.is_file() or p.stat().st_size == 0:
                    return  # Missing or empty file — do not claim (#158)
            except OSError:
                return
            if path not in self.artifacts:
                self.artifacts.append(path)

    # -- Memory persistence (#481) ------------------------------------------

    def _persist_memory(
        self,
        session_id: str,
        task_description: str,
        messages: list[dict],
    ) -> None:
        """Extract key learnings from conversation and persist to memory store.

        Runs after the agent loop completes. Silently skips if memory
        manager is not available or memory is disabled.
        """
        if not self._memory_manager or not self._memory_manager.config.enabled:
            return
        if not self._memory_manager.config.auto_store:
            return

        try:
            self._memory_manager.extract_and_store(
                agent_type="shared",
                task_description=task_description,
                execution_result={
                    "artifacts": self.artifacts,
                    "message_count": len(messages),
                },
                session_id=session_id,
            )
        except Exception as exc:
            logger.debug("Memory persistence failed: %s", exc)
