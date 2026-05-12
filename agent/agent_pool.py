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
from typing import Any

from core.models import AgentMessage
from core.models import DAGNode, HandoffArtifact, AgentCapability
from core.config import LLMConfig
from core.agent_registry import AgentRegistry
from core.llm_router import LLMRouter
from core.exceptions import PendingApprovalError
from session.store import SessionStore
from agent.worker import AgentWorker
from tools.registry import ToolRegistry
from guardrails.policy import Guardrails, GuardrailResult
from core.models import ToolResult


@dataclass(frozen=True)
class ExecutionContext:
    """Explicit per-node execution context passed through the tool call chain.

    Replaces the former pattern of storing run_id/node_id on WorkerAgent
    instance fields, which was unsafe under concurrent execution.
    """

    job_id: str = ""
    run_id: str | None = None
    node_id: str | None = None
    approval_repo: Any | None = None


class WorkerAgent:
    """
    A single Worker Agent instance with isolated context.

    This wraps AgentWorker with:
    - Agent-type-specific system prompt
    - Context isolation (reset between tasks)
    - Guardrails enforcement on tool calls
    - Artifact collection
    """

    SYSTEM_PROMPTS = {
        "planner": """You are the Planner Agent in a software development team.

Your role: Analyze requirements, decompose tasks, design architecture.

You have access to tools: read, glob, grep.

Rules:
1. Produce structured, actionable plans
2. Define clear success criteria for each task
3. Identify dependencies and risks
4. Consider existing codebase before planning changes
5. Output: plan.md, spec.md, architecture decision records

Always consider the project context and existing conventions.
""",

        "generator": """You are the Generator Agent in a software development team.

Your role: Implement code according to specifications.

CRITICAL RULES:
- You MUST use write or edit tools to create or modify files.
- Analysis, reading, and understanding are prerequisites — but NOT the deliverable.
- Your task is NOT complete until you have created or modified at least one file.
- If you understand the problem but have not yet modified any file, CONTINUE working.
- For bug fixes: locate the bug, then USE THE EDIT TOOL to fix it.
- For new features: design the solution, then USE THE WRITE TOOL to create files.

You have access to tools: read, write, edit, bash, glob, grep, git.

Rules:
1. Follow the plan precisely
2. Read existing code before modifying
3. Use edit tool for small changes (old_string → new_string)
4. Use write tool for new files
5. Run tests after implementation
6. Follow project coding standards (import order, naming, formatting).
   CRITICAL: Maximum line length is 100 characters (flake8 --max-line-length=100).
   Break long lines using parenthesized expressions, multi-line f-strings,
   or implicit string concatenation. Avoid backslash continuation.
   CRITICAL: Remove ALL unused imports (F401) and unused variables (F841)
   before finishing. Review imports and delete any not actually used.
7. CRITICAL: If evaluation feedback from a previous attempt is provided,
   read it carefully and fix ALL reported issues before proceeding.
   The feedback tells you exactly what failed and why.
8. Handle edge cases in ALL code you write:
   - Null/None/empty values for function parameters
   - Empty lists, dicts, strings
   - Invalid input types and boundary conditions
9. For data processing functions, add explicit type checking and None handling
10. After writing code, run tests yourself: `python -m pytest -v --tb=short`
    If tests fail, fix the issues before finishing.
11. Prefer creating NEW independent files over modifying existing core files.
    Do NOT edit core infrastructure (models.py, registry.py, config.py,
    plan_validator.py) unless the task explicitly requires it.
12. For library/module tasks, create self-contained files that do not
    import from or depend on the project's internal modules.
13. When modifying enums, constants, or shared definitions:
    - FIRST use grep to find ALL references across the codebase.
    - List every file that references the changed symbol.
    - Update ALL reference sites systematically (mappings, validators,
      tests, specs).
    - Verify completeness: grep -r "SYMBOL_NAME" . --include="*.py"
      should return 0 stale references.
14. FILE PATH CONTRACT: If the task or plan specifies an exact file path
    (e.g., "create reporter/report_engine.py"), you MUST create the file
    at that EXACT path. Do NOT silently substitute a different filename.
    If you believe a different path is better, first create the file at
    the required path, then explain your reasoning in your output.
15. TRUST TOOL RESULTS: When write or edit returns success, the change is
    applied. Do NOT immediately read the same file to verify. Only re-read
    if you need surrounding context not shown in the tool result, or if a
    later test/lint error references that file. Prefer running targeted
    tests or lint over re-reading whole files to confirm edits.
16. CROSS-NODE NAMING: If your task description includes a "NAMING CONTRACT"
    or specifies exact class/function names, you MUST use those exact names.
    Do NOT invent alternative names. If the task says "class TokenBucket",
    create "class TokenBucket" — not "TokenBucketLimiter" or "Token_Bucket".
17. TEST GENERATION: When writing tests for code created by another node,
    FIRST read the source files (use glob to find them, then read) to discover
    the exact class names, function signatures, and module paths. NEVER guess
    class names — always verify by reading the actual source code first.

Work systematically: gather context → implement → verify.
""",

        "evaluator": """You are the Evaluator Agent in a software development team.

Your role: Assess quality, run tests, provide structured feedback.

You have access to tools: read, bash, glob, grep.

Rules:
1. Be strict but constructive
2. Run all relevant tests
3. Check code quality (lint, type safety, coverage)
4. Provide explicit PASS/FAIL verdict
5. Feedback must be specific and actionable

Evaluate against:
- Functional correctness (tests pass)
- Code quality (lint clean, typed)
- Architecture alignment (follows project patterns)
- Edge cases handled
""",
    }

    # Agent-type-specific tool allowlists
    TOOL_ALLOWLIST = {
        "planner": {"read", "glob", "grep"},
        "generator": {"read", "write", "edit", "bash", "glob", "grep", "git"},
        "evaluator": {"read", "bash", "glob", "grep"},
    }

    def __init__(
        self,
        capability: AgentCapability,
        llm_config: LLMConfig,
        session_store: SessionStore,
        tool_registry: ToolRegistry,
        guardrails: Guardrails | None = None,
        max_iterations: int = 50,
        timeout: int = 300,
        max_context_tokens: int = 100_000,
        memory_manager: Any | None = None,
        job_id: str = "",
        approval_repo: Any | None = None,
    ):
        self.capability = capability
        self.llm_config = llm_config
        self.session_store = session_store
        self.tool_registry = tool_registry
        self.guardrails = guardrails
        self.max_iterations = max_iterations
        self.timeout = timeout
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
        )
        self.system_prompt = system_prompt

        # Filter tools by agent type
        allowed = self.TOOL_ALLOWLIST.get(capability.id, {"read", "glob", "grep"})
        self.tools = [s for s in tool_registry.schemas if s["name"] in allowed]

    def _build_runtime_context(self) -> str:
        """Build runtime environment context for agent prompt.

        Injects OS, CWD, project root, and path rules so the agent
        doesn't waste iterations guessing paths (#144).
        """
        import platform
        import sys

        project_root = getattr(self.tool_registry, "base_cwd", None)
        if project_root:
            project_root = str(project_root)
        else:
            from pathlib import Path
            project_root = str(Path.cwd().resolve())

        os_name = platform.system()
        path_sep = "\\" if os_name == "Windows" else "/"

        return (
            f"## Runtime Environment\n"
            f"- OS: {os_name} {platform.release()}\n"
            f"- PROJECT_ROOT: {project_root}\n"
            f"- Python: {sys.executable}\n"
            f"- Path separator: '{path_sep}'\n"
            f"\n"
            f"Path rules:\n"
            f"- All file paths must be relative to PROJECT_ROOT.\n"
            f"- For bash commands, use: python -m pytest tests/test_x.py -v\n"
            f"- Do NOT invent absolute paths like /home/user or C:\\Users\\.\n"
            f"- The workspace is already at PROJECT_ROOT; do NOT cd elsewhere.\n"
        )

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
        )

    async def _execute_inner(
        self,
        task: str,
        input_artifacts: list[HandoffArtifact],
        session_id: str,
        node_id: str | None = None,
        context: ExecutionContext | None = None,
    ) -> dict[str, Any]:
        """Internal execute implementation."""
        # Inject runtime environment context so LLM doesn't guess paths (#144)
        runtime_context = self._build_runtime_context()

        # Build context from input artifacts
        artifact_context = self._format_artifacts(input_artifacts)

        # Inject runtime environment context (#144)
        runtime_context = self._build_runtime_context()

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
                    "You MUST analyze the feedback above and try a DIFFERENT approach.\n"
                    "Do NOT repeat the same tool calls that failed before.\n"
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
        result = await self._run_with_tools(full_prompt, session_id, context)

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
    ) -> dict[str, Any]:
        """Run agent loop and collect results via AgentWorker."""

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
                )
            )

        # AgentWorker.run() is synchronous (blocks on LLM API calls);
        # offload to a thread and cap total wall-clock time per node.
        try:
            messages = await asyncio.wait_for(
                asyncio.to_thread(_run_sync),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            return {
                "status": "timeout",
                "summary": (
                    f"Agent execution timed out after {self.timeout}s. "
                    f"Consider increasing HARNESS_AGENT_TIMEOUT (current: {self.timeout}s)"
                ),
                "artifacts": self.worker.artifacts if hasattr(self, 'worker') else [],
                "output": "",
            }

        final = messages[-1] if messages else None
        return {
            "status": "completed",
            "summary": final.content if final else "",
            "artifacts": self.worker.artifacts,
            "output": final.content if final else "",
        }

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
        timeout: int = 300,
        max_context_tokens: int = 100_000,
        llm_router: LLMRouter | None = None,
        memory_manager: Any | None = None,
        job_id: str = "",
        approval_repo: Any | None = None,
        run_id: str | None = None,
    ):
        self.llm_config = llm_config
        self.session_store = session_store
        self.agent_registry = agent_registry
        self.tool_registry = tool_registry or ToolRegistry()
        self.guardrails = guardrails
        self.max_iterations = max_iterations
        self.timeout = timeout
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

    def create_worker(self, agent_type: str) -> WorkerAgent:
        """Create a fresh WorkerAgent instance for the given type.

        Always creates a new instance to prevent concurrent context pollution
        when multiple nodes of the same agent_type execute in parallel.
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
            tool_registry=self.tool_registry,
            guardrails=self.guardrails,
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

        async def _executor(node: DAGNode, artifacts: list[HandoffArtifact]) -> dict:
            worker = self.create_worker(node.agent_type)
            try:
                return await worker.execute(
                    node.task_description, artifacts, session_id,
                    node_id=node.id,
                    run_id=self.run_id,
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
                        tool_registry=self.tool_registry,
                        guardrails=self.guardrails,
                        max_iterations=self.max_iterations,
                        timeout=self.timeout,
                        max_context_tokens=self.max_context_tokens,
                        memory_manager=self.memory_manager,
                        job_id=self.job_id,
                        approval_repo=self.approval_repo,
                    )
                    try:
                        return await fallback_worker.execute(
                            node.task_description, artifacts, session_id,
                            node_id=node.id,
                            run_id=self.run_id,
                        )
                    except Exception as retry_exc:
                        if not self._is_api_error(retry_exc):
                            raise
                        last_exc = retry_exc
                        failed_model = fallback.config.model
                        continue

        return _executor
