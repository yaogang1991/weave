"""Tests for orchestrator/adapter.py: Adapter, _is_infrastructure_error.

Covers:
1. Infrastructure errors return abort immediately (no LLM call)
2. Zero-output generator nodes with >3 features trigger replan
3. RateLimitError handling: retry/skip/abort based on retry count and deps
4. LLM parse error fallbacks: retry if retries remain, else skip/abort
5. Replan: collects executed node summaries, calls LLM, validates agents
6. _is_infrastructure_error: various patterns
7. Edge cases: empty error, no dependents, max retries exhausted
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from core.models import (
    DAG,
    DAGEdge,
    DAGNode,
    DependencyType,
    FailureDecision,
    NodeStatus,
    OrchestratorPlan,
)
from core.exceptions import RateLimitError
from orchestrator.adapter import (
    Adapter,
    INFRASTRUCTURE_ERROR_PATTERNS,
    _KNOWN_TOOL_COMMANDS,
    _is_infrastructure_error,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dag(
    failed_node_id: str = "gen1",
    agent_type: str = "generator",
    task_description: str = "implement feature",
    error: str = "some error",
    status: NodeStatus = NodeStatus.FAILED,
    retry_count: int = 0,
    max_retries: int = 3,
    node_result: dict | None = None,
    output_artifacts: list[str] | None = None,
) -> DAG:
    """Build a minimal DAG with one failed node."""
    dag = DAG()
    dag = dag.add_node(DAGNode(
        id=failed_node_id,
        agent_type=agent_type,
        task_description=task_description,
        status=status,
        error=error,
        retry_count=retry_count,
        max_retries=max_retries,
        result=node_result or {},
        output_artifacts=output_artifacts or [],
    ))
    return dag


def _make_dag_with_deps(
    failed_node_id: str = "gen1",
    dep_id: str = "eval1",
    dep_type: DependencyType = DependencyType.HARD,
    error: str = "some error",
    retry_count: int = 0,
) -> DAG:
    """Build a DAG with a failed node and one downstream dependent."""
    dag = DAG()
    dag = dag.add_node(DAGNode(
        id=failed_node_id,
        agent_type="generator",
        task_description="implement feature",
        status=NodeStatus.FAILED,
        error=error,
        retry_count=retry_count,
    ))
    dag = dag.add_node(DAGNode(
        id=dep_id,
        agent_type="evaluator",
        task_description="evaluate results",
        status=NodeStatus.PENDING,
    ))
    dag = dag.add_edge(failed_node_id, dep_id, dependency_type=dep_type)
    return dag


def _make_adapter(
    llm_response: dict | None = None,
    llm_side_effect: Exception | None = None,
) -> Adapter:
    """Create an Adapter with mocked dependencies."""
    llm = MagicMock()
    if llm_side_effect:
        llm.call = MagicMock(side_effect=llm_side_effect)
    elif llm_response:
        llm.call = MagicMock(return_value=llm_response)
    else:
        llm.call = MagicMock(return_value={"content": '{"action": "retry", "reasoning": "default"}'})

    llm_config = MagicMock()
    llm_config.model = "test-model"

    agent_registry = MagicMock()
    agent_registry.to_prompt_description.return_value = "Agents: planner, generator, evaluator"
    agent_registry.has_agent.return_value = True
    agent_registry.list_agents.return_value = []

    # Use a mock prompt registry that returns format-compatible templates
    # for each prompt name (adaptation vs replan have different format keys).
    _ADAPTATION_TEMPLATE = (
        "Node {node_id} failed. Agent: {agent_type}. Task: {task}. "
        "Error: {error}. Retries: {retry_count}. DAG: {dag_status}"
    )
    _REPLAN_TEMPLATE = (
        "Executed: {executed_nodes}. Failed: {failed_node}. "
        "Error: {failed_error}. Agents: {agent_descriptions}"
    )

    def _load_prompt(name: str) -> str:
        if name == "replan":
            return _REPLAN_TEMPLATE
        return _ADAPTATION_TEMPLATE

    prompt_registry = MagicMock()
    prompt_registry.load.side_effect = _load_prompt

    plan_to_dag = MagicMock()

    return Adapter(
        llm=llm,
        llm_config=llm_config,
        agent_registry=agent_registry,
        prompt_registry=prompt_registry,
        plan_to_dag_fn=plan_to_dag,
    )


# ===========================================================================
# _is_infrastructure_error
# ===========================================================================


class TestIsInfrastructureError:

    def test_empty_string_returns_false(self):
        assert _is_infrastructure_error("") is False

    def test_none_like_empty_returns_false(self):
        assert _is_infrastructure_error("") is False

    def test_permission_denied(self):
        assert _is_infrastructure_error("Permission denied: /root/file.py") is True

    def test_connection_refused(self):
        assert _is_infrastructure_error("Connection refused on port 8080") is True

    def test_connection_timed_out(self):
        assert _is_infrastructure_error("Connection timed out after 30s") is True

    def test_no_linter_available(self):
        assert _is_infrastructure_error("No linter available for this project") is True

    def test_pytest_not_installed(self):
        assert _is_infrastructure_error("pytest not installed, cannot run tests") is True

    def test_no_python_interpreter(self):
        assert _is_infrastructure_error("No python interpreter found") is True

    def test_case_insensitive_matching(self):
        assert _is_infrastructure_error("PERMISSION DENIED") is True
        assert _is_infrastructure_error("Connection Refused") is True

    def test_command_not_found_known_tool(self):
        assert _is_infrastructure_error("python: command not found") is True
        assert _is_infrastructure_error("command not found: pytest") is True
        assert _is_infrastructure_error("git: command not found") is True
        assert _is_infrastructure_error("npm: command not found") is True

    def test_command_not_found_unknown_tool(self):
        assert _is_infrastructure_error("my_custom_tool: command not found") is False

    def test_command_not_found_no_tool_prefix(self):
        assert _is_infrastructure_error("command not found") is False

    def test_normal_error_not_infrastructure(self):
        assert _is_infrastructure_error("SyntaxError: invalid syntax") is False
        assert _is_infrastructure_error("Test failed: expected 200 got 404") is False

    def test_partial_match_does_not_trigger(self):
        # "permission" alone without "denied" should not match
        assert _is_infrastructure_error("permission was granted") is False

    def test_all_infrastructure_patterns(self):
        for pattern in INFRASTRUCTURE_ERROR_PATTERNS:
            assert _is_infrastructure_error(pattern) is True, (
                f"Pattern '{pattern}' should be detected as infrastructure error"
            )

    def test_all_known_tool_commands(self):
        for tool in _KNOWN_TOOL_COMMANDS:
            assert _is_infrastructure_error(f"{tool}: command not found") is True, (
                f"Tool '{tool}' should be recognized"
            )
            assert _is_infrastructure_error(f"command not found: {tool}") is True, (
                f"Tool '{tool}' should be recognized (reverse form)"
            )


# ===========================================================================
# adapt_to_failure -- infrastructure errors
# ===========================================================================


class TestAdaptToFailureInfrastructure:

    async def test_infrastructure_error_returns_abort(self):
        adapter = _make_adapter()
        dag = _make_dag(error="Permission denied: cannot write to /root")

        decision = await adapter.adapt_to_failure(dag, "gen1", error="Permission denied")

        assert decision.action == "abort"
        assert "Infrastructure error" in decision.reasoning
        # LLM should NOT be called
        adapter.llm.call.assert_not_called()

    async def test_infrastructure_error_from_node_field(self):
        adapter = _make_adapter()
        dag = _make_dag(error="pytest not installed")

        decision = await adapter.adapt_to_failure(dag, "gen1")

        assert decision.action == "abort"
        adapter.llm.call.assert_not_called()

    async def test_infrastructure_error_truncates_long_message(self):
        adapter = _make_adapter()
        long_error = "Permission denied: " + "x" * 500
        dag = _make_dag(error=long_error)

        decision = await adapter.adapt_to_failure(dag, "gen1")

        assert decision.action == "abort"
        # Reasoning contains truncated error (first 200 chars)
        assert len(decision.reasoning) < len(long_error) + 50


# ===========================================================================
# adapt_to_failure -- zero output replan
# ===========================================================================


class TestAdaptToFailureZeroOutput:

    async def test_zero_output_generator_with_many_features_replans(self):
        adapter = _make_adapter()
        # Task with >3 enumerated features
        task = (
            "implement: 1) auth, 2) user management, "
            "3) billing, 4) notifications"
        )
        dag = _make_dag(
            error="zero output produced",
            agent_type="generator",
            task_description=task,
        )

        decision = await adapter.adapt_to_failure(dag, "gen1")

        assert decision.action == "replan"
        assert "zero output" in decision.reasoning.lower() or "too complex" in decision.reasoning.lower()
        adapter.llm.call.assert_not_called()

    async def test_zero_output_generator_few_features_goes_to_llm(self):
        adapter = _make_adapter()
        # Task with <=3 features
        task = "implement auth and user login"
        dag = _make_dag(
            error="zero output produced",
            agent_type="generator",
            task_description=task,
        )

        decision = await adapter.adapt_to_failure(dag, "gen1")

        # Should NOT replan, should call LLM instead
        adapter.llm.call.assert_called_once()
        # Decision comes from LLM response
        assert decision.action == "retry"

    async def test_zero_output_non_generator_goes_to_llm(self):
        adapter = _make_adapter()
        dag = _make_dag(
            failed_node_id="plan1",
            error="zero output produced",
            agent_type="planner",
            task_description="plan the architecture with features 1) a 2) b 3) c 4) d",
        )

        decision = await adapter.adapt_to_failure(dag, "plan1")

        # planner is not generator, so zero-output shortcut does not apply
        adapter.llm.call.assert_called_once()


# ===========================================================================
# adapt_to_failure -- RateLimitError handling
# ===========================================================================


class TestAdaptOfFailureRateLimitError:

    async def test_rate_limit_with_retries_remaining_returns_retry(self):
        exc = RateLimitError(provider="anthropic", model="claude-sonnet-4-6", retries=3)
        adapter = _make_adapter(llm_side_effect=exc)
        dag = _make_dag(retry_count=0, max_retries=3)

        decision = await adapter.adapt_to_failure(dag, "gen1")

        assert decision.action == "retry"
        assert "Rate limit" in decision.reasoning

    async def test_rate_limit_no_retries_soft_deps_returns_skip(self):
        exc = RateLimitError(provider="anthropic", model="claude-sonnet-4-6", retries=3)
        adapter = _make_adapter(llm_side_effect=exc)
        dag = _make_dag_with_deps(
            retry_count=3,
            dep_type=DependencyType.SOFT,
        )

        decision = await adapter.adapt_to_failure(dag, "gen1")

        assert decision.action == "skip"
        assert "soft" in decision.reasoning.lower()

    async def test_rate_limit_no_retries_hard_deps_returns_abort(self):
        exc = RateLimitError(provider="anthropic", model="claude-sonnet-4-6", retries=3)
        adapter = _make_adapter(llm_side_effect=exc)
        dag = _make_dag_with_deps(
            retry_count=3,
            dep_type=DependencyType.HARD,
        )

        decision = await adapter.adapt_to_failure(dag, "gen1")

        assert decision.action == "abort"
        assert "max retries" in decision.reasoning.lower()

    async def test_rate_limit_no_retries_no_deps_returns_abort(self):
        exc = RateLimitError(provider="anthropic", model="claude-sonnet-4-6", retries=3)
        adapter = _make_adapter(llm_side_effect=exc)
        dag = _make_dag(retry_count=3, max_retries=3)

        decision = await adapter.adapt_to_failure(dag, "gen1")

        assert decision.action == "abort"


# ===========================================================================
# adapt_to_failure -- LLM parse error fallbacks
# ===========================================================================


class TestAdaptOfFailureParseError:

    async def test_parse_error_with_retries_remaining_returns_retry(self):
        # LLM returns unparseable content
        adapter = _make_adapter(llm_response={"content": "not valid json at all"})
        dag = _make_dag(retry_count=0, max_retries=3)

        decision = await adapter.adapt_to_failure(dag, "gen1")

        assert decision.action == "retry"
        assert "Parse error" in decision.reasoning

    async def test_parse_error_exhausted_soft_deps_returns_skip(self):
        adapter = _make_adapter(llm_response={"content": "not valid json"})
        dag = _make_dag_with_deps(
            retry_count=3,
            dep_type=DependencyType.SOFT,
        )

        decision = await adapter.adapt_to_failure(dag, "gen1")

        assert decision.action == "skip"
        assert "soft" in decision.reasoning.lower()

    async def test_parse_error_exhausted_hard_deps_returns_abort(self):
        adapter = _make_adapter(llm_response={"content": "not valid json"})
        dag = _make_dag_with_deps(
            retry_count=3,
            dep_type=DependencyType.HARD,
        )

        decision = await adapter.adapt_to_failure(dag, "gen1")

        assert decision.action == "abort"
        assert "max retries" in decision.reasoning.lower()

    async def test_parse_error_no_deps_exhausted_returns_abort(self):
        adapter = _make_adapter(llm_response={"content": "not valid json"})
        dag = _make_dag(retry_count=3, max_retries=3)

        decision = await adapter.adapt_to_failure(dag, "gen1")

        assert decision.action == "abort"


# ===========================================================================
# adapt_to_failure -- successful LLM call
# ===========================================================================


class TestAdaptOfFailureSuccessfulLLM:

    async def test_llm_returns_valid_decision(self):
        adapter = _make_adapter(llm_response={
            "content": json.dumps({
                "action": "retry",
                "reasoning": "Transient failure, retrying",
            }),
        })
        dag = _make_dag()

        decision = await adapter.adapt_to_failure(dag, "gen1")

        assert decision.action == "retry"
        assert decision.reasoning == "Transient failure, retrying"

    async def test_llm_receives_retry_exhausted_message(self):
        adapter = _make_adapter(llm_response={
            "content": json.dumps({
                "action": "skip",
                "reasoning": "Skip this node",
            }),
        })
        dag = _make_dag(retry_count=3, max_retries=3)

        decision = await adapter.adapt_to_failure(dag, "gen1")

        assert decision.action == "skip"
        # Verify LLM was called (the message about exhausted retries is embedded)
        adapter.llm.call.assert_called_once()
        call_args = adapter.llm.call.call_args
        messages = call_args[0][0]  # first positional arg
        # Should have 3 messages: system, user prompt, retry exhausted warning
        assert len(messages) == 3
        assert "exhausted ALL retries" in messages[2]["content"]

    async def test_llm_receives_dag_status(self):
        adapter = _make_adapter(llm_response={
            "content": json.dumps({
                "action": "abort",
                "reasoning": "too many failures",
            }),
        })
        dag = _make_dag()
        dag = dag.add_node(DAGNode(
            id="plan1",
            agent_type="planner",
            task_description="plan things",
            status=NodeStatus.SUCCESS,
        ))

        decision = await adapter.adapt_to_failure(dag, "gen1")

        assert decision.action == "abort"
        # Verify the system prompt was built with DAG status
        adapter.llm.call.assert_called_once()


# ===========================================================================
# adapt_to_failure -- downstream dependency info in prompt
# ===========================================================================


class TestAdaptOfFailureDownstreamInfo:

    async def test_downstream_info_included_in_prompt(self):
        adapter = _make_adapter(llm_response={
            "content": json.dumps({"action": "skip", "reasoning": "ok"}),
        })
        dag = _make_dag_with_deps(dep_type=DependencyType.SOFT)

        decision = await adapter.adapt_to_failure(dag, "gen1")

        assert decision.action == "skip"
        call_args = adapter.llm.call.call_args
        messages = call_args[0][0]
        system_msg = messages[0]["content"]
        # The system prompt should contain downstream info
        assert "Downstream from gen1" in system_msg
        assert "soft dependency" in system_msg

    async def test_hard_dependency_noted_in_prompt(self):
        adapter = _make_adapter(llm_response={
            "content": json.dumps({"action": "abort", "reasoning": "hard dep"}),
        })
        dag = _make_dag_with_deps(dep_type=DependencyType.HARD)

        decision = await adapter.adapt_to_failure(dag, "gen1")

        call_args = adapter.llm.call.call_args
        messages = call_args[0][0]
        system_msg = messages[0]["content"]
        assert "hard dependency" in system_msg


# ===========================================================================
# replan
# ===========================================================================


class TestReplan:

    async def test_replan_collects_executed_node_summaries(self):
        plan = OrchestratorPlan(
            reasoning="replanned",
            nodes=[{"id": "new_gen", "agent_type": "generator", "task": "new task"}],
            edges=[],
        )
        mock_dag = DAG()
        mock_dag = mock_dag.add_node(DAGNode(id="new_gen", agent_type="generator", task_description="new task"))

        adapter = _make_adapter(llm_response={
            "content": json.dumps(plan.model_dump()),
        })
        adapter._plan_to_dag.return_value = mock_dag

        dag = DAG()
        dag = dag.add_node(DAGNode(
            id="gen1", agent_type="generator",
            task_description="original task",
            status=NodeStatus.FAILED,
            error="boom",
            result={"summary": "partial work"},
        ))
        dag = dag.add_node(DAGNode(
            id="plan1", agent_type="planner",
            task_description="plan",
            status=NodeStatus.SUCCESS,
            result={"summary": "planned"},
        ))

        result = await adapter.replan(dag, "gen1", requirement="build API")

        # LLM was called with executed summaries
        adapter.llm.call.assert_called_once()
        call_args = adapter.llm.call.call_args
        messages = call_args[0][0]
        system_msg = messages[0]["content"]
        assert "gen1" in system_msg
        assert "plan1" in system_msg

    async def test_replan_validates_agents(self):
        plan = OrchestratorPlan(
            reasoning="replanned",
            nodes=[{"id": "x", "agent_type": "unknown_agent", "task": "do thing"}],
            edges=[],
        )

        adapter = _make_adapter(llm_response={
            "content": json.dumps(plan.model_dump()),
        })
        adapter.agent_registry.has_agent.return_value = False
        adapter.agent_registry.list_agents.return_value = [
            MagicMock(id="generator"), MagicMock(id="planner"),
        ]

        dag = _make_dag(status=NodeStatus.FAILED, error="fail")

        with pytest.raises(ValueError, match="unregistered agent"):
            await adapter.replan(dag, "gen1")

    async def test_replan_rate_limit_returns_original_dag(self):
        exc = RateLimitError(provider="anthropic", model="test", retries=3)
        adapter = _make_adapter(llm_side_effect=exc)
        dag = _make_dag(status=NodeStatus.FAILED, error="fail")

        result = await adapter.replan(dag, "gen1")

        # Should return the original DAG unchanged
        assert result is dag

    async def test_replan_parse_failure_raises_value_error(self):
        # All attempts return unparseable content
        adapter = _make_adapter(llm_response={"content": "not json"})
        dag = _make_dag(status=NodeStatus.FAILED, error="fail")

        with pytest.raises(ValueError, match="Failed to parse replanning"):
            await adapter.replan(dag, "gen1")

    async def test_replan_successful_returns_new_dag(self):
        plan = OrchestratorPlan(
            reasoning="replanned",
            nodes=[{"id": "new_gen", "agent_type": "generator", "task": "new task"}],
            edges=[],
        )
        mock_dag = DAG()
        mock_dag = mock_dag.add_node(DAGNode(id="new_gen", agent_type="generator", task_description="new task"))

        adapter = _make_adapter(llm_response={
            "content": json.dumps(plan.model_dump()),
        })
        adapter._plan_to_dag.return_value = mock_dag

        dag = _make_dag(status=NodeStatus.FAILED, error="fail")
        result = await adapter.replan(dag, "gen1", requirement="new requirement")

        assert result is mock_dag
        adapter._plan_to_dag.assert_called_once()

    async def test_replan_truncates_large_artifact_lists(self):
        adapter = _make_adapter(llm_response={
            "content": json.dumps({
                "reasoning": "r",
                "nodes": [{"id": "n1", "agent_type": "generator", "task": "t"}],
                "edges": [],
            }),
        })
        mock_dag = DAG()
        mock_dag = mock_dag.add_node(DAGNode(id="n1", agent_type="generator", task_description="t"))
        adapter._plan_to_dag.return_value = mock_dag

        dag = DAG()
        dag = dag.add_node(DAGNode(
            id="gen1",
            agent_type="generator",
            task_description="big output",
            status=NodeStatus.SUCCESS,
            output_artifacts=[f"file_{i}.py" for i in range(50)],
        ))

        await adapter.replan(dag, "gen1")

        # The LLM prompt should include truncated artifact list
        call_args = adapter.llm.call.call_args
        messages = call_args[0][0]
        system_msg = messages[0]["content"]
        # Should contain the truncation marker
        assert "...and " in system_msg

    async def test_replan_includes_failed_error(self):
        adapter = _make_adapter(llm_response={
            "content": json.dumps({
                "reasoning": "r",
                "nodes": [{"id": "n1", "agent_type": "generator", "task": "t"}],
                "edges": [],
            }),
        })
        mock_dag = DAG()
        mock_dag = mock_dag.add_node(DAGNode(id="n1", agent_type="generator", task_description="t"))
        adapter._plan_to_dag.return_value = mock_dag

        dag = DAG()
        dag = dag.add_node(DAGNode(
            id="gen1",
            agent_type="generator",
            task_description="fail task",
            status=NodeStatus.FAILED,
            error="specific failure details here",
        ))

        await adapter.replan(dag, "gen1")

        call_args = adapter.llm.call.call_args
        messages = call_args[0][0]
        system_msg = messages[0]["content"]
        assert "specific failure details here" in system_msg

    async def test_replan_retries_on_parse_failure_then_succeeds(self):
        # First call returns unparseable content, second returns valid JSON
        plan = OrchestratorPlan(
            reasoning="replanned",
            nodes=[{"id": "n1", "agent_type": "generator", "task": "t"}],
            edges=[],
        )
        adapter = _make_adapter()
        adapter.llm.call = MagicMock(side_effect=[
            {"content": "not json on first attempt"},
            {"content": json.dumps(plan.model_dump())},
        ])
        mock_dag = DAG()
        mock_dag = mock_dag.add_node(DAGNode(id="n1", agent_type="generator", task_description="t"))
        adapter._plan_to_dag.return_value = mock_dag

        dag = _make_dag(status=NodeStatus.FAILED, error="fail")
        result = await adapter.replan(dag, "gen1")

        assert result is mock_dag
        assert adapter.llm.call.call_count == 2

    async def test_replan_unknown_failed_node_empty_error(self):
        adapter = _make_adapter(llm_response={
            "content": json.dumps({
                "reasoning": "r",
                "nodes": [{"id": "n1", "agent_type": "generator", "task": "t"}],
                "edges": [],
            }),
        })
        mock_dag = DAG()
        mock_dag = mock_dag.add_node(DAGNode(id="n1", agent_type="generator", task_description="t"))
        adapter._plan_to_dag.return_value = mock_dag

        dag = DAG()
        dag = dag.add_node(DAGNode(
            id="other_node",
            agent_type="generator",
            task_description="other",
            status=NodeStatus.FAILED,
        ))

        # failed_node_id not in dag -- should handle gracefully (empty error)
        result = await adapter.replan(dag, "nonexistent_node")

        assert result is mock_dag


# ===========================================================================
# _prune_messages delegation
# ===========================================================================


class TestPruneMessages:

    def test_prune_messages_delegates_to_llm_utils(self):
        adapter = _make_adapter()
        messages = [{"role": "system", "content": "hello"}]

        with patch("orchestrator.adapter.prune_messages_for_tokens", return_value=messages) as mock_tok, \
             patch("orchestrator.adapter.prune_messages_for_size", return_value=messages) as mock_size:
            result = adapter._prune_messages(messages)
            mock_size.assert_called_once_with(messages)
            mock_tok.assert_called_once_with(messages, "test-model")


# ===========================================================================
# Adapter __init__
# ===========================================================================


class TestAdapterInit:

    def test_stores_dependencies(self):
        llm = MagicMock()
        llm_config = MagicMock()
        agent_registry = MagicMock()
        prompt_registry = MagicMock()
        plan_to_dag = MagicMock()

        adapter = Adapter(llm, llm_config, agent_registry, prompt_registry, plan_to_dag)

        assert adapter.llm is llm
        assert adapter.llm_config is llm_config
        assert adapter.agent_registry is agent_registry
        assert adapter._prompt_registry is prompt_registry
        assert adapter._plan_to_dag is plan_to_dag


# ===========================================================================
# adapt_to_failure -- edge cases
# ===========================================================================


class TestAdaptOfFailureEdgeCases:

    async def test_error_param_overrides_node_error_when_empty(self):
        adapter = _make_adapter()
        # Node has no error set, but error param is passed
        dag = _make_dag(error="")
        dag.nodes["gen1"].error = ""

        decision = await adapter.adapt_to_failure(dag, "gen1", error="pytest not installed")

        assert decision.action == "abort"
        adapter.llm.call.assert_not_called()

    async def test_node_error_takes_precedence_over_param(self):
        adapter = _make_adapter()
        dag = _make_dag(error="pytest not installed")
        # Even if param has different error, node.error should be used
        decision = await adapter.adapt_to_failure(dag, "gen1", error="some other error")

        assert decision.action == "abort"
        assert "pytest not installed" in decision.reasoning

    async def test_dag_with_no_downstream_deps(self):
        adapter = _make_adapter(llm_response={
            "content": json.dumps({"action": "retry", "reasoning": "try again"}),
        })
        dag = _make_dag()  # Single node, no edges

        decision = await adapter.adapt_to_failure(dag, "gen1")

        assert decision.action == "retry"
        call_args = adapter.llm.call.call_args
        messages = call_args[0][0]
        system_msg = messages[0]["content"]
        # No downstream info should be present
        assert "Downstream from" not in system_msg

    async def test_llm_call_uses_planner_max_tokens(self):
        adapter = _make_adapter(llm_response={
            "content": json.dumps({"action": "retry", "reasoning": "ok"}),
        })
        dag = _make_dag()

        await adapter.adapt_to_failure(dag, "gen1")

        call_kwargs = adapter.llm.call.call_args[1]
        assert "max_tokens_override" in call_kwargs
        assert call_kwargs["max_tokens_override"] == 8192
