"""Tests for #910: provider health key uses correct model from LLMConfig.

Previously _get_provider_model() extracted from NodeTimeoutConfig which has
no model field, producing health key "anthropic/" instead of
"anthropic/claude-sonnet-4-6".
"""

from core.config import LLMConfig
from core.dag_engine import DAGExecutionEngine, DAGEngineConfig


def _make_engine(llm_config=None) -> DAGExecutionEngine:
    async def _agent_exec(node, artifacts):
        return {}

    async def _failure_handler(dag, node_id, error):
        from core.models import FailureDecision
        return FailureDecision(action="abort", reasoning="test")

    return DAGExecutionEngine(
        agent_executor=_agent_exec,
        failure_handler=_failure_handler,
        config=DAGEngineConfig(),
        llm_config=llm_config,
    )


class TestGetProviderModel:
    def test_extracts_from_llm_config(self):
        cfg = LLMConfig(model="claude-sonnet-4-6", provider="anthropic")
        engine = _make_engine(llm_config=cfg)
        provider, model = engine._get_provider_model()
        assert provider == "anthropic"
        assert model == "claude-sonnet-4-6"

    def test_extracts_openai_provider(self):
        cfg = LLMConfig(model="gpt-4o", provider="openai")
        engine = _make_engine(llm_config=cfg)
        provider, model = engine._get_provider_model()
        assert provider == "openai"
        assert model == "gpt-4o"

    def test_fallback_when_no_llm_config(self):
        engine = _make_engine(llm_config=None)
        provider, model = engine._get_provider_model()
        assert provider == "anthropic"
        assert model == ""

    def test_backward_compat_no_param(self):
        """Engine created without llm_config should still work."""
        async def _agent_exec(node, artifacts):
            return {}

        async def _failure_handler(dag, node_id, error):
            from core.models import FailureDecision
            return FailureDecision(action="abort", reasoning="test")

        engine = DAGExecutionEngine(
            agent_executor=_agent_exec,
            failure_handler=_failure_handler,
        )
        provider, model = engine._get_provider_model()
        assert provider == "anthropic"
