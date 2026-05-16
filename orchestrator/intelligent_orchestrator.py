"""
Intelligent Orchestrator: An LLM-driven agent that plans and manages multi-agent execution.

Key design principles:
1. AGNOSTIC: Does NOT hardcode agent types - discovers via AgentRegistry
2. DYNAMIC: Generates DAG at runtime based on user requirements
3. ADAPTIVE: Handles failures by replanning, not hardcoded rules
4. MINIMAL DEFAULT: Only assumes planner/generator/evaluator exist

The orchestrator itself IS an agent - it uses LLM to reason about task decomposition
and agent assignment, but it does NOT execute tasks itself.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from core.models import (
    DAG,
    DAGNode,
    DAGEdge,
    DependencyType,
    FailureDecision,
    AgentCapability,
    OrchestratorPlan,
    SuccessCriterion,
)
from core.agent_registry import AgentRegistry
from core.config import LLMConfig
from core.llm_client import LLMClient
from core.llm_router import LLMRouter
from session.store import SessionStore
from orchestrator.plan_validator import PlanValidator, PlanValidationError
from orchestrator.prompts import PromptRegistry, get_prompt_registry

logger = logging.getLogger(__name__)
from templates.library import TemplateRegistry


# Infrastructure errors cannot be fixed by retrying with the same environment.
# Detect early and abort instead of wasting retry budget (#187).
INFRASTRUCTURE_ERROR_PATTERNS: list[str] = [
    # Explicit tool/command missing — always infra
    "no linter available",
    "pytest not installed",
    "no python interpreter",
    # Network / permission — always infra
    "permission denied",
    "connection refused",
    "connection timed out",
]

# Known infrastructure tool commands — only "command not found" for these
# counts as an infrastructure error.  Other missing commands (project CLIs,
# make targets, etc.) may be fixable by the agent via retry.
_KNOWN_TOOL_COMMANDS: list[str] = [
    "python",
    "python3",
    "pytest",
    "flake8",
    "ruff",
    "autopep8",
    "pip",
    "node",
    "npm",
    "git",
]


def _is_infrastructure_error(error: str) -> bool:
    """Check whether an error is an infrastructure/environment issue
    that cannot be resolved by retrying."""
    if not error:
        return False
    lower = error.lower()
    if any(pattern in lower for pattern in INFRASTRUCTURE_ERROR_PATTERNS):
        return True
    # Check for "command not found" only when it refers to a known tool.
    if "command not found" in lower:
        # Match both "bash: python: command not found" and "command not found: python"
        return any(
            f"{tool}: command not found" in lower
            or f"command not found: {tool}" in lower
            for tool in _KNOWN_TOOL_COMMANDS
        )
    return False


class IntelligentOrchestrator:
    """
    Orchestrator Agent: Plans DAG, monitors execution, adapts to failures.
    
    This is itself an LLM-driven agent, not a hardcoded state machine.
    It queries the AgentRegistry to discover available workers,
    then uses LLM reasoning to decompose tasks and build execution DAGs.
    """

    PLANNING_PROMPT_TEMPLATE: str = ""  # Loaded from file via PromptRegistry
    ADAPTATION_PROMPT_TEMPLATE: str = ""  # Loaded from file via PromptRegistry
    REPLAN_PROMPT_TEMPLATE: str = ""  # Loaded from file via PromptRegistry

    def __init__(
        self,
        llm_config: LLMConfig,
        session_store: SessionStore,
        agent_registry: AgentRegistry,
        llm_router: LLMRouter | None = None,
        learning_optimizer: Any | None = None,
        prompt_registry: PromptRegistry | None = None,
        skill_registry: Any | None = None,
    ):
        self.llm_config = llm_config
        self.session_store = session_store
        self.agent_registry = agent_registry
        self.learning_optimizer = learning_optimizer
        self.skill_registry = skill_registry
        self._prompt_registry = prompt_registry or get_prompt_registry()
        if llm_router:
            self.llm = llm_router.get_client("orchestrator")
        else:
            self.llm = LLMClient(llm_config)

    async def plan(self, requirement: str, project_context: dict | None = None) -> DAG:
        """
        Generate an execution DAG from user requirements.

        This is the core planning method. It:
        1. Discovers available agents from registry
        2. Builds a dynamic planning prompt
        3. Calls LLM to generate the DAG
        4. Validates and returns the DAG
        """
        # Step 1: Discover available agents
        agent_descriptions = self.agent_registry.to_prompt_description()

        # Step 2: Build planning prompt
        planning_template = self._prompt_registry.load("planning")
        system_prompt = planning_template.format(
            agent_descriptions=agent_descriptions
        )

        # Pre-flight token estimation (#417): truncate requirement if the
        # combined prompt is likely to exceed the model's context window.
        # We use a conservative 3.5 chars-per-token estimate and reserve
        # 50% of the window for the model's response.
        requirement = self._truncate_requirement_if_needed(
            requirement, system_prompt, project_context,
        )

        user_prompt = f"User requirement: {requirement}"
        if project_context:
            # Format existing_files as a prominent section so the planner
            # sees it clearly and reconciles rather than duplicating (#335).
            existing = project_context.pop("existing_files", None)
            user_prompt += (
                f"\n\nProject context: "
                f"{json.dumps(project_context, indent=2, default=str)}"
            )
            if existing:
                user_prompt += (
                    "\n\n## Existing Workspace Files\n"
                    "The following files already exist in the workspace. "
                    "You MUST reconcile with them (reuse, edit, or replace) "
                    "instead of creating duplicates:\n"
                )
                for f in existing:
                    user_prompt += f"- [{f['type']}] {f['path']}\n"
                project_context["existing_files"] = existing

            # Inject retry context so the planner knows this is a
            # continuation, not a fresh start (#328).
            retry_attempt = project_context.get("retry_attempt", 0)
            if retry_attempt > 0:
                user_prompt += (
                    f"\n\n## Retry Context\n"
                    f"This is **attempt {retry_attempt + 1}** of "
                    f"{project_context.get('max_attempts', '?')}.\n"
                    f"A previous attempt failed/timed out. "
                    f"{project_context.get('existing_file_count', 0)} "
                    f"files already exist in the workspace from prior work.\n"
                    f"**DO NOT start from scratch.** Build upon existing files. "
                    f"Only create files that don't exist yet. "
                    f"Only plan work that hasn't been completed.\n"
                )

        # M3.3: Inject learning hints if available
        if self.learning_optimizer:
            try:
                hints = self.learning_optimizer.get_planning_hints(requirement)
                if hints:
                    user_prompt += f"\n\n{hints}"
            except Exception:
                pass  # Learning hints must not break planning

        # M3.6: Inject skill descriptions if available
        if self.skill_registry:
            try:
                skills_desc = self.skill_registry.to_prompt_description()
                if skills_desc:
                    user_prompt += f"\n\n{skills_desc}"
            except Exception:
                pass  # Skill descriptions must not break planning

        # Step 3: Call LLM for planning (with retry on JSON parse failure)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        max_retries = 2
        plan_data = None
        for attempt in range(max_retries + 1):
            messages = self._prune_messages_for_size(messages)
            response = self.llm.call(messages, tools=[])
            plan_data = self._extract_json(response.get("content", ""))
            if plan_data is not None:
                break
            if attempt < max_retries:
                # Truncate the failed response to prevent context balloon
                # on retry (#417). Keep first 2000 chars — enough for the
                # model to understand what it generated wrong.
                failed_content = response.get("content", "")
                if len(failed_content) > 2000:
                    failed_content = (
                        failed_content[:2000]
                        + "\n... (truncated for token limit)"
                    )
                messages.append({
                    "role": "assistant",
                    "content": failed_content,
                })
                messages.append({
                    "role": "user",
                    "content": (
                        "Your previous response could not be parsed as valid JSON. "
                        "Please provide the plan as a valid JSON object."
                    ),
                })

        if plan_data is None:
            last_response = response.get("content", "")
            preview = last_response[:500] if last_response else "(empty response)"
            logger.error(
                "Planning response parse failed after %d attempts. "
                "Last LLM output (first 500 chars):\n%s",
                max_retries + 1, preview,
            )
            raise ValueError(
                f"Failed to parse planning response after {max_retries + 1} retries. "
                f"LLM did not return valid JSON.\n"
                f"Last response preview: {preview}"
            )

        # Step 4: Parse the plan
        plan = OrchestratorPlan(**plan_data)

        # Step 5: Validate all agent types exist in registry
        for node_def in plan.nodes:
            agent_type = node_def.get("agent_type", "")
            if not self.agent_registry.has_agent(agent_type):
                raise ValueError(
                    f"Plan references unregistered agent: {agent_type}. "
                    f"Available: {[a.id for a in self.agent_registry.list_agents()]}"
                )

        # Step 6: Structural validation with retry on node-count error (#292)
        validator = PlanValidator(auto_fix=True)
        try:
            fixed_plan_data = validator.validate(plan.model_dump())
        except PlanValidationError as e:
            err_msg = str(e)
            if "nodes" in err_msg.lower() and "maximum" in err_msg.lower():
                # Retry once with explicit node-limit feedback
                logger.warning(
                    "Plan has too many nodes, retrying with constraint: %s",
                    err_msg,
                )
                node_resp = response.get("content", "")
                if len(node_resp) > 2000:
                    node_resp = (
                        node_resp[:2000]
                        + "\n... (truncated for message size limit)"
                    )
                messages.append({
                    "role": "assistant",
                    "content": node_resp,
                })
                messages.append({
                    "role": "user",
                    "content": (
                        f"Validation error: {err_msg}. "
                        "Reduce the plan to at most 10 nodes by combining "
                        "related sub-tasks, and return a valid JSON plan."
                    ),
                })
                messages = self._prune_messages_for_size(messages)
                response = self.llm.call(messages, tools=[])
                plan_data = self._extract_json(response.get("content", ""))
                if plan_data is None:
                    raise ValueError(
                        "Failed to parse replan response after node-limit error. "
                        f"Original error: {err_msg}"
                    )
                plan = OrchestratorPlan(**plan_data)
                # Re-validate agent types
                for node_def in plan.nodes:
                    agent_type = node_def.get("agent_type", "")
                    if not self.agent_registry.has_agent(agent_type):
                        raise ValueError(
                            f"Plan references unregistered agent: {agent_type}. "
                            f"Available: {[a.id for a in self.agent_registry.list_agents()]}"
                        )
                fixed_plan_data = validator.validate(plan.model_dump())
            else:
                raise

        if validator.warnings:
            for w in validator.warnings:
                print(f"[PlanValidator] {w}")
        plan = OrchestratorPlan(**fixed_plan_data)

        # Step 7: Convert to DAG
        dag = self._plan_to_dag(plan)

        # Step 8: Apply stdlib rename map to criterion paths (#285)
        if validator.rename_map:
            self._apply_rename_map(dag, validator.rename_map)

        return dag

    async def plan_from_template(
        self,
        template_name: str,
        variables: dict[str, str] | None = None,
    ) -> DAG:
        """
        Generate a DAG from a named template with variable substitution.

        This bypasses LLM planning for known task patterns.
        """
        registry = TemplateRegistry()
        dag = registry.instantiate(template_name, variables)

        # Validate agent types
        for nid, node in dag.nodes.items():
            if not self.agent_registry.has_agent(node.agent_type):
                raise ValueError(
                    f"Template references unregistered agent: {node.agent_type}. "
                    f"Available: {[a.id for a in self.agent_registry.list_agents()]}"
                )
        return dag

    async def adapt_to_failure(self, dag: DAG, failed_node_id: str, error: str = "") -> FailureDecision:
        """
        Handle a failed node by asking the orchestrator LLM to decide.

        This is the adaptive part - the orchestrator reasons about the failure
        and decides the best course of action, rather than using hardcoded rules.
        """
        failed_node = dag.nodes[failed_node_id]

        # Infrastructure errors (missing tools, broken env) are never fixable
        # by retrying — abort immediately instead of wasting retry budget (#187).
        node_error = failed_node.error or error
        if _is_infrastructure_error(node_error):
            logger.warning(
                "Node %s failed with infrastructure error, aborting: %s",
                failed_node_id, node_error[:200],
            )
            return FailureDecision(
                action="abort",
                reasoning=f"Infrastructure error (not retryable): {node_error[:200]}",
            )

        # Zero-output + complex task → auto-replan without wasting LLM call (#409).
        # When a generator node produces zero artifacts because the task has too many
        # distinct features, retrying the same node is futile. Replan immediately.
        is_zero_output = "zero output" in node_error.lower()
        if is_zero_output and failed_node.agent_type == "generator":
            feature_count = self._count_features(failed_node.task_description)
            if feature_count > 3:
                logger.info(
                    "Node %s: zero output with %d features, auto-replanning",
                    failed_node_id, feature_count,
                )
                return FailureDecision(
                    action="replan",
                    reasoning=(
                        f"Node produced zero output artifacts with {feature_count} "
                        f"distinct features. Task is too complex for a single node. "
                        f"Split into 2-3 smaller nodes with shared foundation."
                    ),
                )

        # Build DAG status summary
        dag_status = []
        for nid, node in dag.nodes.items():
            dag_status.append(
                f"- {nid}: {node.agent_type} = {node.status.value}"
                f"{' (FAILED: ' + node.error[:300] + ')' if node.status.value == 'failed' else ''}"
            )

        # Topology awareness: describe downstream dependency types (#271)
        dependents = dag.get_dependents(failed_node_id)
        downstream_info: list[str] = []
        has_only_soft_dependents = True
        for dep_id in dependents:
            dep_edges = [e for e in dag.edges
                         if e.from_node == failed_node_id and e.to_node == dep_id]
            for edge in dep_edges:
                dtype = edge.dependency_type.value
                downstream_info.append(f"  → {dep_id} ({dtype} dependency)")
                if edge.dependency_type == DependencyType.HARD:
                    has_only_soft_dependents = False
        if downstream_info:
            dag_status.append(f"Downstream from {failed_node_id}:")
            dag_status.extend(downstream_info)

        # Build adaptation prompt
        system_prompt = self._prompt_registry.load("adaptation").format(
            node_id=failed_node_id,
            agent_type=failed_node.agent_type,
            task=failed_node.task_description,
            error=failed_node.error[:2000],
            retry_count=failed_node.retry_count,
            dag_status="\n".join(dag_status),
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Handle failure of node {failed_node_id}"},
        ]

        response = self.llm.call(messages, tools=[])

        try:
            decision_data = self._extract_json(response.get("content", ""))
            return FailureDecision(**decision_data)
        except Exception:
            # Topology-aware fallback: soft-only dependents → skip, not abort (#271)
            if failed_node.retry_count < failed_node.max_retries:
                return FailureDecision(action="retry", reasoning="Parse error, defaulting to retry")
            if has_only_soft_dependents and dependents:
                return FailureDecision(
                    action="skip",
                    reasoning="Parse error; all downstream deps are soft, skipping failed node",
                )
            return FailureDecision(action="abort", reasoning="Parse error, max retries reached")

    @staticmethod
    def _count_features(task_description: str) -> int:
        """Count distinct complex features in a task description (#409).

        Uses the same heuristic as PlanValidator._estimate_feature_count
        to detect enumerated lists and feature patterns.
        """
        return PlanValidator._estimate_feature_count(task_description)

    def _plan_to_dag(self, plan: OrchestratorPlan) -> DAG:
        """Convert an OrchestratorPlan to an executable DAG."""
        dag = DAG(reasoning=plan.reasoning)

        for node_def in plan.nodes:
            node = DAGNode(
                id=node_def["id"],
                agent_type=node_def["agent_type"],
                task_description=node_def["task"],
                success_criteria=node_def.get("success_criteria", []),
                owned_files=node_def.get("owned_files", []),
            )
            dag.add_node(node)

        for edge_def in plan.edges:
            dep_type_str = edge_def.get("dependency_type", "hard")
            dep_type = (
                DependencyType.SOFT if dep_type_str == "soft"
                else DependencyType.HARD
            )
            dag.add_edge(edge_def["from"], edge_def["to"], dependency_type=dep_type)

        return dag

    @staticmethod
    def _apply_rename_map(dag: DAG, rename_map: dict[str, str]) -> None:
        """Update criterion paths and task descriptions when stdlib shadowing
        triggered a rename (#422).

        For each node, scan success_criteria for file_exists / file_pattern
        paths that contain a stdlib-conflicting name and replace it with the
        prefixed alternative.  Also rewrites plain-string criteria that
        contain the conflicting name as a path segment, and updates task
        descriptions to match.
        """
        for node in dag.nodes.values():
            updated: list[str | SuccessCriterion] = []
            for crit in node.success_criteria:
                if isinstance(crit, SuccessCriterion):
                    new_path = crit.path
                    new_pattern = crit.pattern
                    for old, new in rename_map.items():
                        new_path = new_path.replace(f"/{old}/", f"/{new}/")
                        new_path = new_path.replace(f"/{old}.", f"/{new}.")
                        new_pattern = new_pattern.replace(f"/{old}/", f"/{new}/")
                        new_pattern = new_pattern.replace(f"/{old}.", f"/{new}.")
                    if new_path != crit.path or new_pattern != crit.pattern:
                        crit = crit.model_copy(update={
                            "path": new_path,
                            "pattern": new_pattern,
                        })
                    updated.append(crit)
                elif isinstance(crit, str):
                    s = crit
                    for old, new in rename_map.items():
                        s = s.replace(f"/{old}/", f"/{new}/")
                        s = s.replace(f"/{old}.", f"/{new}.")
                    updated.append(s)
                else:
                    updated.append(crit)
            node.success_criteria = updated

            # Also update task description so the generator creates files
            # with the renamed paths (#422).  Use word-boundary matching
            # to avoid double-renaming already-prefixed names.
            for old, new in rename_map.items():
                # Replace `old.py` preceded by a non-word char or start-of-string
                task = node.task_description
                result = []
                i = 0
                target = f"{old}.py"
                while i < len(task):
                    pos = task.find(target, i)
                    if pos == -1:
                        result.append(task[i:])
                        break
                    # Check preceding character is not a word char (avoids
                    # matching "app_numbers.py" when old="numbers").
                    # Underscores count as word chars here.
                    if pos == 0 or not (task[pos - 1].isalnum() or task[pos - 1] == '_'):
                        result.append(task[i:pos])
                        result.append(f"{new}.py")
                        i = pos + len(target)
                    else:
                        result.append(task[i:pos + len(target)])
                        i = pos + len(target)
                node.task_description = "".join(result)

    # -- Token limit protection (#417) ----------------------------------------

    # Known model context windows (in tokens).  Used for pre-flight
    # estimation so we don't send requests that are guaranteed to fail.
    _MODEL_CONTEXT_WINDOWS: dict[str, int] = {
        "claude-sonnet-4-6": 200_000,
        "claude-opus-4-6": 200_000,
        "claude-haiku-4-5": 200_000,
        "gpt-4o": 128_000,
        "gpt-4-turbo": 128_000,
        "o1": 200_000,
        "o3": 200_000,
        "o4-mini": 200_000,
    }
    _DEFAULT_CONTEXT_WINDOW = 200_000
    # Conservative chars-per-token estimate (English + code mix).
    _CHARS_PER_TOKEN = 3.5
    # Anthropic API total message size limit (bytes).  We prune at 80% to
    # leave headroom for the response and API envelope overhead (#419).
    _MAX_MESSAGE_BYTES = 2_097_152  # 2 MiB
    _PRUNE_THRESHOLD = 0.80

    def _estimate_tokens(self, text: str) -> int:
        """Rough token estimate: chars / 3.5."""
        return int(len(text) / self._CHARS_PER_TOKEN)

    def _get_context_window(self) -> int:
        """Get the context window for the configured model."""
        model = self.llm_config.model
        for key, window in self._MODEL_CONTEXT_WINDOWS.items():
            if key in model:
                return window
        return self._DEFAULT_CONTEXT_WINDOW

    def _truncate_requirement_if_needed(
        self,
        requirement: str,
        system_prompt: str,
        project_context: dict | None,
    ) -> str:
        """Truncate requirement if the combined prompt is likely to exceed
        the model's context window (#417).

        Reserves 50% of the window for the model's response.
        Returns the (possibly truncated) requirement.
        """
        system_tokens = self._estimate_tokens(system_prompt)

        # Estimate context overhead (project_context JSON, existing_files, etc.)
        context_text = ""
        if project_context:
            context_text = json.dumps(project_context, default=str)
        context_tokens = self._estimate_tokens(context_text)

        requirement_tokens = self._estimate_tokens(requirement)

        total_estimated = system_tokens + context_tokens + requirement_tokens
        context_window = self._get_context_window()

        # Reserve 50% for the model's response
        max_input_tokens = context_window // 2

        if total_estimated <= max_input_tokens:
            return requirement

        # Need to truncate — calculate how many tokens we can afford
        overhead_tokens = system_tokens + context_tokens
        remaining_tokens = max_input_tokens - overhead_tokens

        if remaining_tokens <= 0:
            logger.warning(
                "System prompt + context (%d tokens) already exceeds "
                "half the context window (%d tokens). Sending requirement "
                "as-is — token limit error is likely.",
                overhead_tokens, max_input_tokens,
            )
            return requirement

        # Convert remaining token budget to chars
        max_chars = int(remaining_tokens * self._CHARS_PER_TOKEN)
        logger.warning(
            "Requirement too long (%d chars, ~%d tokens). "
            "Truncating to %d chars to fit context window (%d tokens).",
            len(requirement), requirement_tokens, max_chars, context_window,
        )

        truncated = requirement[:max_chars]
        # Try to cut at a reasonable boundary (double newline)
        last_boundary = truncated.rfind("\n\n")
        if last_boundary > max_chars // 2:
            truncated = truncated[:last_boundary]

        truncated += (
            "\n\n[NOTE: The original requirement was truncated from "
            f"{len(requirement)} to {len(truncated)} chars to fit the "
            f"model's context window. Focus on the most critical parts "
            f"and produce a minimal viable plan.]"
        )
        return truncated

    # -- Message size management (#419) ------------------------------------

    @staticmethod
    def _estimate_messages_bytes(messages: list[dict]) -> int:
        """Estimate total byte size of messages payload (UTF-8)."""
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            total += len(content.encode("utf-8", errors="replace"))
            # Account for role key + dict overhead (~50 bytes per message)
            total += 50
        return total

    def _prune_messages_for_size(
        self,
        messages: list[dict],
    ) -> list[dict]:
        """Prune messages to stay within the Anthropic 2 MiB limit (#419).

        Strategy:
        1. Keep the system prompt (index 0) and the last user message intact.
        2. Truncate intermediate assistant messages to a summary.
        3. If still too large, truncate the user message content.
        """
        max_bytes = int(self._MAX_MESSAGE_BYTES * self._PRUNE_THRESHOLD)
        current = self._estimate_messages_bytes(messages)

        if current <= max_bytes:
            return messages

        logger.warning(
            "Messages payload %d bytes exceeds %d byte threshold — pruning.",
            current, max_bytes,
        )

        # Work on a copy to avoid mutating the original
        pruned = [dict(m) for m in messages]

        # Pass 1: truncate assistant messages (indices 1..n-1, skipping last)
        for i in range(1, len(pruned) - 1):
            if pruned[i].get("role") == "assistant":
                content = pruned[i].get("content", "")
                if len(content) > 2000:
                    pruned[i] = {
                        "role": "assistant",
                        "content": (
                            content[:2000]
                            + "\n... (truncated for message size limit)"
                        ),
                    }

        current = self._estimate_messages_bytes(pruned)
        if current <= max_bytes:
            return pruned

        # Pass 2: truncate user messages (except the first system prompt)
        for i in range(1, len(pruned)):
            if pruned[i].get("role") == "user":
                content = pruned[i].get("content", "")
                if len(content) > 4000:
                    pruned[i] = dict(pruned[i])
                    pruned[i]["content"] = (
                        content[:4000]
                        + "\n... (truncated for message size limit)"
                    )

        current = self._estimate_messages_bytes(pruned)
        if current <= max_bytes:
            return pruned

        # Pass 3: drop intermediate messages entirely, keep only first + last
        if len(pruned) > 2:
            pruned = [pruned[0], pruned[-1]]
            logger.warning(
                "Dropped intermediate messages — keeping only system + last user."
            )

        return pruned

    def _extract_json(self, text: str) -> dict | None:
        """
        Extract JSON from LLM response (handles markdown code blocks).

        Collects candidate substrings from multiple strategies, tries
        json.loads on each, and only attempts repair on truly truncated
        candidates (unclosed braces at end-of-text).

        Returns None when no valid JSON can be extracted.
        """
        text = text.strip()
        candidates: list[str] = []

        # Strategy 1: JSON inside ```json ... ``` blocks
        json_block_match = re.search(r"```json\s*\n(.*?)```", text, re.DOTALL)
        if json_block_match:
            candidates.append(json_block_match.group(1).strip())

        # Strategy 2: JSON inside generic ``` ... ``` blocks
        generic_block_match = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
        if generic_block_match:
            candidate = generic_block_match.group(1).strip()
            if candidate.startswith("{") or candidate.startswith("["):
                candidates.append(candidate)

        # Strategy 3: First top-level JSON object via brace matching
        brace_depth = 0
        start = None
        for i, ch in enumerate(text):
            if ch == '{':
                if brace_depth == 0:
                    start = i
                brace_depth += 1
            elif ch == '}':
                brace_depth -= 1
                if brace_depth == 0 and start is not None:
                    candidates.append(text[start:i + 1])
                    start = None

        # Strategy 4: Truncated JSON (unclosed braces at end-of-text)
        if start is not None and brace_depth > 0:
            candidates.append(self._repair_truncated_json(text[start:], brace_depth))

        # Try each candidate in order — first valid wins
        for candidate in candidates:
            try:
                result = json.loads(candidate)
                if isinstance(result, dict):
                    return result
            except (json.JSONDecodeError, ValueError):
                continue

        return None

    @staticmethod
    def _repair_truncated_json(text: str, brace_depth: int) -> str:
        """Attempt to close a truncated JSON object by appending missing
        closing quotes and braces.  Only handles genuine truncation
        (unclosed braces), not complete-but-malformed JSON."""
        # Close unclosed quotes (odd number of unescaped ")
        quote_count = text.count('"') - text.count('\\"')
        if quote_count % 2:
            text += '"'
        # Close unclosed braces
        text += '}' * brace_depth
        return text

    async def replan(self, dag: DAG, failed_node_id: str, requirement: str = "") -> DAG:
        """
        Re-plan the remaining work after a node failure.

        This method:
        1. Collects a summary of already-executed nodes (both successful and failed)
        2. Builds a replanning prompt with execution context
        3. Calls the LLM to generate a new plan for the remaining work
        4. Validates and converts the plan to a DAG
        5. Returns the new DAG (to be merged with the old one by the engine)

        Args:
            dag: The current DAG with execution status populated.
            failed_node_id: The ID of the node that triggered the replan.
            requirement: The original user requirement for context.

        Returns:
            A new DAG containing only the nodes that still need execution.
        """
        # 1. Collect executed node summaries
        executed_summary: list[dict[str, Any]] = []
        for nid, node in dag.nodes.items():
            if node.status.value in ("success", "failed"):
                executed_summary.append({
                    "id": nid,
                    "agent_type": node.agent_type,
                    "status": node.status.value,
                    "task": node.task_description[:100],
                    "result_summary": (
                        node.result.get("summary", "")[:200]
                        if node.result else ""
                    ),
                })

        # 2. Build replan prompt
        failed_error = dag.nodes[failed_node_id].error[:500] if failed_node_id in dag.nodes else ""
        system_prompt = self._prompt_registry.load("replan").format(
            executed_nodes=json.dumps(executed_summary, indent=2),
            failed_node=failed_node_id,
            failed_error=failed_error,
            agent_descriptions=self.agent_registry.to_prompt_description(),
        )

        user_prompt = (
            f"Original requirement: {requirement}\n\n"
            f"Failed node: {failed_node_id}\n\n"
            f"Please generate a new plan for the remaining work."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # 3. Call LLM (with retry on JSON parse failure)
        max_retries = 2
        plan_data = None
        for attempt in range(max_retries + 1):
            messages = self._prune_messages_for_size(messages)
            response = self.llm.call(messages, tools=[])
            plan_data = self._extract_json(response.get("content", ""))
            if plan_data is not None:
                break
            if attempt < max_retries:
                replan_resp = response.get("content", "")
                if len(replan_resp) > 2000:
                    replan_resp = (
                        replan_resp[:2000]
                        + "\n... (truncated for message size limit)"
                    )
                messages.append({
                    "role": "assistant",
                    "content": replan_resp,
                })
                messages.append({
                    "role": "user",
                    "content": (
                        "Your previous response could not be parsed as valid JSON. "
                        "Please provide the plan as a valid JSON object."
                    ),
                })

        if plan_data is None:
            raise ValueError(
                "Failed to parse replanning response after retries. "
                "The LLM did not return valid JSON."
            )

        # 4. Parse and validate
        plan = OrchestratorPlan(**plan_data)

        for node_def in plan.nodes:
            agent_type = node_def.get("agent_type", "")
            if not self.agent_registry.has_agent(agent_type):
                raise ValueError(
                    f"Replan references unregistered agent: {agent_type}. "
                    f"Available: {[a.id for a in self.agent_registry.list_agents()]}"
                )

        # 5. Convert to DAG and return
        new_dag = self._plan_to_dag(plan)
        return new_dag
