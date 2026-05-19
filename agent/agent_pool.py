"""
Agent Pool: Manages multiple independent Agent instances.

Each Worker Agent gets:
- Independent LLM context (no shared context window)
- Isolated tool registry (subset of tools based on agent type)
- Guardrails enforcement on every tool call
- Independent session tracking within the global session
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.models import AgentMessage
from core.models import DAGNode, HandoffArtifact, AgentCapability
from core.config import LLMConfig
from core.agent_registry import AgentRegistry
from core.llm_router import LLMRouter
from core.exceptions import PendingApprovalError
from control_plane.approval import ApprovalRepository
from memory.manager import MemoryManager
from session.store import SessionStore
from agent.worker import AgentWorker
from tools.registry import ToolRegistry
from guardrails.policy import Guardrails, GuardrailResult
from core.models import ToolResult
from agent.prompts import SYSTEM_PROMPTS, TOOL_ALLOWLIST


@dataclass(frozen=True)
class ExecutionContext:
    """Explicit per-node execution context passed through the tool call chain.

    Replaces the former pattern of storing run_id/node_id on WorkerAgent
    instance fields, which was unsafe under concurrent execution.
    """

    job_id: str = ""
    run_id: str | None = None
    node_id: str | None = None
    approval_repo: ApprovalRepository | None = None


class WorkerAgent:
    """
    A single Worker Agent instance with isolated context.

    This wraps AgentWorker with:
    - Agent-type-specific system prompt
    - Context isolation (reset between tasks)
    - Guardrails enforcement on tool calls
    - Artifact collection
    """

    SYSTEM_PROMPTS = SYSTEM_PROMPTS

    # Agent-type-specific tool allowlists
    TOOL_ALLOWLIST = TOOL_ALLOWLIST

    def __init__(
        self,
        capability: AgentCapability,
        llm_config: LLMConfig,
        session_store: SessionStore,
        tool_registry: ToolRegistry,
        guardrails: Guardrails | None = None,
        max_iterations: int = 50,
        timeout: int = 300,  # Deprecated: timeout managed by dag_engine (#360)
        max_context_tokens: int = 100_000,
        memory_manager: MemoryManager | None = None,
        job_id: str = "",
        approval_repo: ApprovalRepository | None = None,
    ):
        self.capability = capability
        self.llm_config = llm_config
        self.session_store = session_store
        self.tool_registry = tool_registry
        self.guardrails = guardrails
        self.max_iterations = max_iterations
        self.timeout = timeout  # Deprecated: not used for execution (#360)
        self.memory_manager = memory_manager
        self.job_id = job_id
        self.approval_repo = approval_repo

        # Build agent-specific system prompt
        system_prompt = capability.system_prompt or self.SYSTEM_PROMPTS.get(
            capability.id,
            f"You are the {capability.name} agent. {capability.description}"
        )

        base_cwd = str(tool_registry.base_cwd) if getattr(tool_registry, "base_cwd", None) else None
        self.worker = AgentWorker(
            llm_config, session_store,
            max_context_tokens=max_context_tokens,
            base_cwd=base_cwd,
            memory_manager=memory_manager,
        )
        self.system_prompt = system_prompt

        # Filter tools by agent type
        allowed = self.TOOL_ALLOWLIST.get(capability.id, {"read", "glob", "grep"})
        self.tools = [s for s in tool_registry.schemas if s["name"] in allowed]

    def _execute_tool(
        self,
        name: str,
        arguments: dict,
        context: ExecutionContext,
    ) -> ToolResult:
        """Execute a tool through guardrails with approval context.

        Uses check_and_execute() which:
        - Returns ToolResult when allowed (executed) or blocked
        - Returns GuardrailResult when pending_approval (ticket created)
        When pending_approval, raises PendingApprovalError to propagate up
        through DAGEngine → RunService → Worker.
        """
        if self.guardrails:
            result = self.guardrails.check_and_execute(
                name, arguments,
                job_id=context.job_id or self.job_id,
                run_id=context.run_id,
                approval_repo=context.approval_repo or self.approval_repo,
                node_id=context.node_id,
            )
            if isinstance(result, GuardrailResult):
                if result.is_pending:
                    raise PendingApprovalError(
                        ticket_id=result.ticket_id or "",
                        guardrail_result=result,
                    )
                # Blocked
                return ToolResult(
                    tool_call_id="",
                    success=False,
                    error=f"Blocked by guardrails: {result.reason}",
                )
            return result  # ToolResult from successful execution
        return self.tool_registry.execute(name, arguments)

    async def execute(
        self,
        task: str,
        input_artifacts: list[HandoffArtifact],
        session_id: str,
        node_id: str | None = None,
        run_id: str | None = None,
        cancel_event: asyncio.Event | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """
        Execute this agent's task with isolated context.

        Context isolation: Each execution starts fresh - previous
        executions do not pollute the context window.
        """
        context = ExecutionContext(
            job_id=self.job_id,
            run_id=run_id,
            node_id=node_id,
            approval_repo=self.approval_repo,
        )
        return await self._execute_inner(
            task, input_artifacts, session_id, node_id, context,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )

    async def _execute_inner(
        self,
        task: str,
        input_artifacts: list[HandoffArtifact],
        session_id: str,
        node_id: str | None = None,
        context: ExecutionContext | None = None,
        cancel_event: asyncio.Event | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Internal execute implementation."""
        # Inject runtime environment context so LLM doesn't guess paths (#144)
        runtime_context = self._build_runtime_context()

        # Build context from input artifacts
        artifact_context = self._format_artifacts(input_artifacts)

        # Detect retry and inject differentiation instruction
        retry_instruction = ""
        for a in input_artifacts:
            if (
                hasattr(a, "metadata")
                and a.metadata
                and a.metadata.get("type") == "eval_feedback"
            ):
                retry_instruction = (
                    "\n## IMPORTANT: This is a RETRY attempt.\n"
                    "Your previous attempt was evaluated and FAILED. "
                    "You MUST analyze the feedback above and fix the issues.\n\n"
                    "INCREMENTAL FIX RULES:\n"
                    "1. Do NOT rewrite files from scratch — your previous code "
                    "was mostly correct\n"
                    "2. Use the EDIT tool to fix ONLY the specific failing "
                    "tests/functions mentioned in the feedback\n"
                    "3. If a test expects a different exception type, change "
                    "ONLY that assertion\n"
                    "4. If a test expects a different return value, change "
                    "ONLY that assertion\n"
                    "5. Do NOT modify tests that were PASSING — they don't "
                    "need changes\n"
                    "6. After editing, run ONLY the failing tests to verify: "
                    "`python -m pytest path/to/test.py -v`\n"
                    "7. SOURCE CODE FIXES: If the feedback shows tests failing "
                    "because of bugs in the SOURCE code (RuntimeError, "
                    "AttributeError, wrong return values, missing method calls), "
                    "you MAY also EDIT the source files to fix those bugs. "
                    "Make targeted fixes — do NOT rewrite the entire module (#288).\n"
                    "8. FIXTURE/CONFIG ISSUES: If ALL tests fail with import or "
                    "fixture errors (e.g. fixture not found, dependency injection "
                    "mismatch), read conftest.py and the source code first, then "
                    "fix the fixture configuration to match the actual API (#599).\n"
                )
                break

        # M3.2: Inject memory context if memory manager is available
        memory_section = ""
        if self.memory_manager and self.memory_manager.config.enabled:
            memory_entries = self.memory_manager.get_context_for_agent(
                agent_type=self.capability.id,
                task_description=task,
                session_id=session_id,
            )
            memory_section = self.memory_manager.format_memory_prompt(memory_entries)

        full_prompt = f"""{runtime_context}
{artifact_context}
{retry_instruction}
{memory_section}
Your task: {task}

Execute using your available tools. Produce clear, verifiable output.
"""

        # Run the agent (dumb loop) via AgentWorker
        result = await self._run_with_tools(
            full_prompt, session_id, context,
            node_id=node_id or "",
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )

        # M3.2: Store learnings from execution result
        if (
            self.memory_manager
            and self.memory_manager.config.enabled
            and self.memory_manager.config.auto_store
            and node_id
        ):
            try:
                self.memory_manager.extract_and_store(
                    agent_type=self.capability.id,
                    task_description=task,
                    execution_result=result,
                    session_id=session_id,
                    node_id=node_id,
                )
            except Exception as e:
                import logging
                logging.getLogger(__name__).debug("Memory extraction failed: %s", e)

        return result

    async def _run_with_tools(
        self,
        prompt: str,
        session_id: str,
        context: ExecutionContext | None = None,
        node_id: str = "",
        cancel_event: asyncio.Event | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Run agent loop and collect results via AgentWorker.

        Timeout is managed by dag_engine._execute_with_timeout (#360 PR2).
        This method handles cooperative cancellation and progress reporting.
        """

        _ctx = context or ExecutionContext()

        # AgentWorker expects a tool_executor with execute(name, arguments) -> ToolResult
        class _ToolExecutor:
            def __init__(self, agent: WorkerAgent, ctx: ExecutionContext):
                self._agent = agent
                self._ctx = ctx

            def execute(self, name: str, arguments: dict):
                return self._agent._execute_tool(
                    name, arguments, self._ctx,
                )

        tool_executor = _ToolExecutor(self, _ctx)

        def _run_sync() -> list[AgentMessage]:
            return list(
                self.worker.run(
                    session_id=session_id,
                    system_prompt=self.system_prompt,
                    user_message=prompt,
                    tools=self.tools,
                    tool_executor=tool_executor,
                    max_iterations=self.max_iterations,
                    cancel_event=cancel_event,
                    progress_callback=progress_callback,
                )
            )

        # No timeout here — dag_engine manages it via _execute_with_timeout.
        # Just run in thread and return result.
        messages = await asyncio.to_thread(_run_sync)

        # Post-generation cleanup: auto-remove unused imports/variables
        # from artifacts before evaluation (#391).  This eliminates the
        # most common cause of node retries (60-70% are F401) so the
        # evaluator sees clean files from the start.  The evaluator also
        # runs autoflake as a safety net, but cleaning here avoids the
        # retry-feedback round-trip entirely.
        artifacts = self.worker.artifacts
        if artifacts and self.capability.id == "generator":
            await asyncio.to_thread(
                self._cleanup_unused_imports, artifacts,
            )

        final = messages[-1] if messages else None
        return {
            "status": "completed",
            "summary": final.content if final else "",
            "artifacts": self.worker.artifacts,
            "output": final.content if final else "",
        }

    @staticmethod
    def _cleanup_unused_imports(artifacts: list[str]) -> None:
        """Run autoflake on generated files to remove unused imports (#391).

        This runs after the generator agent loop finishes but before the
        evaluator, so files are clean when lint checking starts.  Silently
        skips if autoflake is not installed or times out.
        """
        import subprocess
        import sys as _sys
        _log = logging.getLogger(__name__)

        py_files = [f for f in artifacts if f.endswith(".py")]
        if not py_files:
            return

        # Resolve relative paths against CWD
        from pathlib import Path
        resolved = []
        for f in py_files:
            p = Path(f)
            if not p.is_absolute():
                p = Path.cwd() / p
            if p.is_file():
                resolved.append(str(p))

        if not resolved:
            return

        try:
            result = subprocess.run(
                [
                    _sys.executable, "-m", "autoflake",
                    "--in-place",
                    "--remove-all-unused-imports",
                    "--remove-unused-variables",
                ] + resolved,
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                _log.info(
                    "Post-generation autoflake cleaned %d file(s) (#391)",
                    len(resolved),
                )
            else:
                _log.debug(
                    "autoflake returned %d for %d file(s): %s",
                    result.returncode, len(resolved),
                    result.stderr[:200] if result.stderr else "",
                )
        except FileNotFoundError:
            _log.debug("autoflake not installed, skipping post-gen cleanup (#391)")
        except subprocess.TimeoutExpired:
            _log.warning("autoflake timed out during post-gen cleanup (#391)")
        except Exception as exc:
            _log.warning("autoflake post-gen cleanup error: %s", exc)

    def _build_runtime_context(self) -> str:
        """Build runtime environment info for agent prompt (#144).

        Injects OS, CWD, PROJECT_ROOT, and PYTHON so the LLM doesn't
        need to guess paths or make platform-specific assumptions.
        """
        import platform
        import sys

        project_root = (
            getattr(self.tool_registry, "base_cwd", None)
            or Path.cwd()
        )
        return (
            "## Runtime Environment\n"
            f"- OS: {platform.system()} {platform.release()}\n"
            f"- CWD: {Path.cwd().resolve()}\n"
            f"- PROJECT_ROOT: {Path(project_root).resolve()}\n"
            f"- PYTHON: {sys.executable}\n"
            "\nPath rules:\n"
            "- Use PROJECT_ROOT as working directory for all bash commands.\n"
            "- Prefer relative paths from PROJECT_ROOT, e.g. "
            "`python -m pytest tests/test_x.py -v`.\n"
            "- Do not invent paths like /home/user on Windows.\n"
            "- Do not cd into unknown directories; "
            "bash already runs inside the project workspace.\n"
        )

    def _format_artifacts(self, artifacts: list[HandoffArtifact]) -> str:
        """Format input artifacts as context for the agent."""
        if not artifacts:
            return ""

        parts = ["## Input from previous agents:"]
        for artifact in artifacts:
            parts.append(f"\n### From {artifact.from_agent}:")
            parts.append(f"Summary: {artifact.content}")
            if artifact.file_paths:
                parts.append(f"Files: {', '.join(artifact.file_paths)}")
        return "\n".join(parts)


def _inject_file_path_constraints(node: DAGNode) -> str:
    """Inject explicit file path constraints into the task description (#291).

    When a generator node has file_exists or file_pattern success_criteria,
    prepend a strong constraint that the LLM must create files at exactly
    those paths. This prevents the generator from using alternative filenames
    that don't match the evaluator's expectations.
    """
    if node.agent_type != "generator":
        return node.task_description

    from core.models import CriterionType
    from evaluator.compat import normalize_criteria

    criteria = normalize_criteria(node.success_criteria)
    paths: list[str] = []
    for crit in criteria:
        if crit.type == CriterionType.FILE_EXISTS and crit.path:
            paths.extend(p.strip() for p in crit.path.split(","))
        elif crit.type == CriterionType.FILE_PATTERN and crit.pattern:
            paths.append(crit.pattern)

    if not paths:
        return node.task_description

    constraint = (
        "CRITICAL FILE PATH CONSTRAINT — you MUST create files at EXACTLY "
        f"these paths: {', '.join(paths)}\n"
        "Do NOT use alternative filenames, different directories, or split "
        "files into separate modules. The evaluator will check for these "
        "exact paths.\n\n"
    )
    return constraint + node.task_description


class AgentPool:
    """
    Pool of Worker Agent instances.

    Creates agent instances on demand based on AgentRegistry capabilities.
    Each instance is independent with isolated context.
    """

    def __init__(
        self,
        llm_config: LLMConfig,
        session_store: SessionStore,
        agent_registry: AgentRegistry,
        tool_registry: ToolRegistry | None = None,
        guardrails: Guardrails | None = None,
        max_iterations: int = 50,
        timeout: int = 300,  # Deprecated: timeout managed by dag_engine (#360)
        max_context_tokens: int = 100_000,
        llm_router: LLMRouter | None = None,
        memory_manager: MemoryManager | None = None,
        job_id: str = "",
        approval_repo: ApprovalRepository | None = None,
        run_id: str | None = None,
    ):
        self.llm_config = llm_config
        self.session_store = session_store
        self.agent_registry = agent_registry
        self.tool_registry = tool_registry or ToolRegistry()
        self.guardrails = guardrails
        self.max_iterations = max_iterations
        self.timeout = timeout  # Deprecated: not used for execution (#360)
        self.max_context_tokens = max_context_tokens
        self.llm_router = llm_router
        self.memory_manager = memory_manager
        self.job_id = job_id
        self.approval_repo = approval_repo
        self.run_id = run_id

    def _is_api_error(self, exc: Exception) -> bool:
        """Check if an exception is likely an LLM API error (auth, rate limit, etc.)."""
        exc_name = type(exc).__name__
        return any(
            substr in exc_name
            for substr in (
                "Authentication", "Permission", "RateLimit",
                "API", "Connection", "ServiceUnavailable",
            )
        )

    def create_worker(
        self,
        agent_type: str,
        tool_registry: ToolRegistry | None = None,
        guardrails: Guardrails | None = None,
    ) -> WorkerAgent:
        """Create a fresh WorkerAgent instance for the given type.

        Always creates a new instance to prevent concurrent context pollution
        when multiple nodes of the same agent_type execute in parallel.

        Args:
            tool_registry: Override tool registry for per-node workspace
                isolation (#176 PR2). When None, uses the pool's shared registry.
            guardrails: Override guardrails for per-node workspace isolation
                (#414). When None, uses the pool's shared guardrails.
        """
        capability = self.agent_registry.get(agent_type)
        if not capability:
            raise ValueError(f"Unknown agent type: {agent_type}")

        # Use router for per-agent-type model selection if available
        if self.llm_router:
            llm_config = self.llm_router.get_client(agent_type).config
        else:
            llm_config = self.llm_config

        return WorkerAgent(
            capability=capability,
            llm_config=llm_config,
            session_store=self.session_store,
            tool_registry=tool_registry or self.tool_registry,
            guardrails=guardrails or self.guardrails,
            max_iterations=self.max_iterations,
            timeout=self.timeout,
            max_context_tokens=self.max_context_tokens,
            memory_manager=self.memory_manager,
            job_id=self.job_id,
            approval_repo=self.approval_repo,
        )

    # Backward-compatible alias
    get_or_create = create_worker

    def get_executor(self, session_id: str):
        """
        Return a callable that the DAG engine can use to execute nodes.

        Signature: async def executor(node, artifacts) -> result_dict

        When the LLM router is available, failures are retried with the
        next model in the fallback chain before propagating to the DAG engine.
        """
        _log = logging.getLogger(__name__)

        async def _executor(
            node: DAGNode,
            artifacts: list[HandoffArtifact],
            cancel_event: asyncio.Event | None = None,
            progress_callback: Callable[[str], None] | None = None,
            workspace_path: str | None = None,
        ) -> dict:
            # Per-node workspace isolation: create ToolRegistry with node's
            # workspace as base_cwd so tools operate in the isolated directory (#176 PR2).
            node_tool_registry = self.tool_registry
            node_guardrails = self.guardrails
            if workspace_path:
                from tools.registry import ToolRegistry as _TR
                # Clone existing registry (preserves MCP/project tools) and
                # redirect base_cwd to the isolated workspace.
                node_tool_registry = _TR(
                    base_cwd=workspace_path,
                    sandbox_runner=self.tool_registry.sandbox_runner,
                )
                for name, handler in self.tool_registry._tools.items():
                    if name not in node_tool_registry._tools:
                        node_tool_registry._tools[name] = handler
                        node_tool_registry._schemas[name] = (
                            self.tool_registry._schemas.get(name, {})
                        )
                # Create per-node Guardrails wrapping the isolated registry
                # so check_and_execute() uses node_tool_registry (#414 re-review).
                if self.guardrails:
                    node_guardrails = Guardrails(
                        self.guardrails.policy, node_tool_registry,
                        project_dir=self.guardrails._project_dir,
                    )
            worker = self.create_worker(
                node.agent_type,
                tool_registry=node_tool_registry,
                guardrails=node_guardrails,
            )

            # Set ownership context on tool registry before execution (#272)
            if node.owned_files:
                node_tool_registry.set_ownership_context({
                    "owned": node.owned_files,
                    "forbidden": getattr(node, '_forbidden_files', []),
                    "shared": getattr(node, '_shared_files', []),
                })
            else:
                node_tool_registry.set_ownership_context(None)

            try:
                task = _inject_file_path_constraints(node)
                return await worker.execute(
                    task, artifacts, session_id,
                    node_id=node.id,
                    run_id=self.run_id,
                    cancel_event=cancel_event,
                    progress_callback=progress_callback,
                )
            except Exception as exc:
                if not self.llm_router or not self._is_api_error(exc):
                    raise

                # Walk the fallback chain, tracking attempted models to prevent cycles
                failed_model = worker.llm_config.model
                attempted: set[str] = {failed_model}
                last_exc = exc
                while True:
                    fallback = self.llm_router.get_fallback_client(failed_model)
                    if fallback is None or fallback.config.model in attempted:
                        raise last_exc
                    attempted.add(fallback.config.model)
                    _log.warning(
                        "Model %s failed (%s), trying fallback %s",
                        failed_model, last_exc, fallback.config.model,
                    )
                    capability = self.agent_registry.get(node.agent_type)
                    fallback_worker = WorkerAgent(
                        capability=capability,
                        llm_config=fallback.config,
                        session_store=self.session_store,
                        tool_registry=node_tool_registry,
                        guardrails=node_guardrails,
                        max_iterations=self.max_iterations,
                        timeout=self.timeout,
                        max_context_tokens=self.max_context_tokens,
                        memory_manager=self.memory_manager,
                        job_id=self.job_id,
                        approval_repo=self.approval_repo,
                    )
                    try:
                        return await fallback_worker.execute(
                            task, artifacts, session_id,
                            node_id=node.id,
                            run_id=self.run_id,
                            cancel_event=cancel_event,
                            progress_callback=progress_callback,
                        )
                    except Exception as retry_exc:
                        if not self._is_api_error(retry_exc):
                            raise
                        last_exc = retry_exc
                        failed_model = fallback.config.model
                        continue
            finally:
                node_tool_registry.set_ownership_context(None)

        return _executor
