"""Tests for M3.1 Multi-Model Routing."""

import os
import pytest
import tempfile
from pathlib import Path

from core.config import LLMConfig, ModelRoute, ModelRoutingConfig, HarnessConfig
from core.llm_router import LLMRouter


class TestModelRoute:
    """Test ModelRoute data model."""

    def test_default_values(self):
        route = ModelRoute()
        assert route.provider == ""
        assert route.model == ""
        assert route.temperature is None
        assert route.max_tokens is None

    def test_with_values(self):
        route = ModelRoute(
            provider="anthropic",
            model="claude-sonnet-4-6",
            temperature=0.1,
            max_tokens=2048,
        )
        assert route.provider == "anthropic"
        assert route.model == "claude-sonnet-4-6"
        assert route.temperature == 0.1
        assert route.max_tokens == 2048


class TestModelRoutingConfig:
    """Test ModelRoutingConfig creation and loading."""

    def test_empty_defaults(self):
        config = ModelRoutingConfig()
        assert config.routing == {}
        assert config.fallback_chain == ["claude-sonnet-4-6"]

    def test_with_routing(self):
        config = ModelRoutingConfig(
            routing={
                "planner": ModelRoute(provider="anthropic", model="claude-opus-4-6"),
                "generator": ModelRoute(provider="anthropic", model="claude-sonnet-4-6"),
            }
        )
        assert "planner" in config.routing
        assert config.routing["planner"].model == "claude-opus-4-6"

    def test_from_env_empty(self):
        config = ModelRoutingConfig.from_env()
        assert config.routing == {}

    def test_from_env_with_vars(self, monkeypatch):
        monkeypatch.setenv("HARNESS_PLANNER_MODEL", "claude-opus-4-6")
        monkeypatch.setenv("HARNESS_GENERATOR_MODEL", "gpt-4o-mini")
        config = ModelRoutingConfig.from_env()
        assert config.routing["planner"].model == "claude-opus-4-6"
        assert config.routing["planner"].provider == "anthropic"
        assert config.routing["generator"].model == "gpt-4o-mini"
        assert config.routing["generator"].provider == "openai"

    def test_from_env_fallback(self, monkeypatch):
        monkeypatch.setenv("HARNESS_MODEL_FALLBACK", "claude-opus-4-6,gpt-4o")
        config = ModelRoutingConfig.from_env()
        assert config.fallback_chain == ["claude-opus-4-6", "gpt-4o"]

    def test_from_yaml(self, tmp_path):
        yaml_content = """
routing:
  planner:
    provider: anthropic
    model: claude-opus-4-6
    temperature: 0.2
  generator: gpt-4o
fallback_chain:
  - claude-opus-4-6
  - gpt-4o
"""
        yaml_file = tmp_path / "routing.yaml"
        yaml_file.write_text(yaml_content)
        config = ModelRoutingConfig.from_yaml(str(yaml_file))
        assert config.routing["planner"].model == "claude-opus-4-6"
        assert config.routing["planner"].temperature == 0.2
        assert config.routing["generator"].model == "gpt-4o"
        assert config.routing["generator"].provider == "openai"
        assert len(config.fallback_chain) == 2


class TestHarnessConfigIntegration:
    """Test that HarnessConfig includes model_routing."""

    def test_default_model_routing(self):
        config = HarnessConfig()
        assert isinstance(config.model_routing, ModelRoutingConfig)
        assert config.model_routing.routing == {}

    def test_from_env_includes_routing(self, monkeypatch):
        monkeypatch.setenv("HARNESS_PLANNER_MODEL", "claude-opus-4-6")
        config = HarnessConfig.from_env()
        assert config.model_routing.routing["planner"].model == "claude-opus-4-6"


class TestLLMRouter:
    """Test LLMRouter routing logic."""

    def _make_base_config(self) -> LLMConfig:
        return LLMConfig(
            provider="anthropic",
            model="claude-sonnet-4-6",
            api_key="test-key",
        )

    def test_no_routing_returns_default(self):
        """When no routing configured, all agents get default model."""
        config = ModelRoutingConfig()
        router = LLMRouter(config, self._make_base_config())
        client = router.get_client("planner")
        assert client.config.model == "claude-sonnet-4-6"
        assert client.config.provider == "anthropic"

    def test_routing_per_agent_type(self):
        """Each agent type gets its configured model."""
        config = ModelRoutingConfig(
            routing={
                "planner": ModelRoute(provider="anthropic", model="claude-opus-4-6"),
                "generator": ModelRoute(provider="anthropic", model="claude-sonnet-4-6"),
                "evaluator": ModelRoute(provider="anthropic", model="claude-opus-4-6"),
            }
        )
        router = LLMRouter(config, self._make_base_config())

        planner_client = router.get_client("planner")
        assert planner_client.config.model == "claude-opus-4-6"

        generator_client = router.get_client("generator")
        assert generator_client.config.model == "claude-sonnet-4-6"

    def test_unknown_agent_gets_default(self):
        """Unknown agent types fall back to default model."""
        config = ModelRoutingConfig(
            routing={
                "planner": ModelRoute(provider="anthropic", model="claude-opus-4-6"),
            }
        )
        router = LLMRouter(config, self._make_base_config())
        client = router.get_client("custom_agent")
        assert client.config.model == "claude-sonnet-4-6"

    def test_client_caching(self):
        """Same (provider, model) pair returns cached client."""
        config = ModelRoutingConfig(
            routing={
                "planner": ModelRoute(provider="anthropic", model="claude-opus-4-6"),
                "evaluator": ModelRoute(provider="anthropic", model="claude-opus-4-6"),
            }
        )
        router = LLMRouter(config, self._make_base_config())

        planner_client = router.get_client("planner")
        evaluator_client = router.get_client("evaluator")
        # Same model -> same cached client instance
        assert planner_client is evaluator_client

    def test_temperature_override(self):
        """Temperature from route overrides base config."""
        config = ModelRoutingConfig(
            routing={
                "planner": ModelRoute(
                    provider="anthropic",
                    model="claude-opus-4-6",
                    temperature=0.1,
                ),
            }
        )
        router = LLMRouter(config, self._make_base_config())
        client = router.get_client("planner")
        assert client.config.temperature == 0.1

    def test_provider_inference_openai(self):
        """gpt-* models are inferred as openai provider."""
        config = ModelRoutingConfig(
            routing={
                "generator": ModelRoute(model="gpt-4o"),
            }
        )
        router = LLMRouter(config, self._make_base_config())
        client = router.get_client("generator")
        assert client.config.provider == "openai"
        assert client.config.model == "gpt-4o"

    def test_fallback_chain(self):
        """Fallback returns next model in chain."""
        config = ModelRoutingConfig(
            fallback_chain=["claude-opus-4-6", "claude-sonnet-4-6", "gpt-4o"]
        )
        router = LLMRouter(config, self._make_base_config())

        fb1 = router.get_fallback_client("claude-opus-4-6")
        assert fb1 is not None
        assert fb1.config.model == "claude-sonnet-4-6"

        fb2 = router.get_fallback_client("claude-sonnet-4-6")
        assert fb2 is not None
        assert fb2.config.model == "gpt-4o"

    def test_fallback_exhausted(self):
        """Returns None when fallback chain is exhausted."""
        config = ModelRoutingConfig(
            fallback_chain=["claude-opus-4-6"]
        )
        router = LLMRouter(config, self._make_base_config())
        result = router.get_fallback_client("claude-opus-4-6")
        assert result is None

    def test_get_routing_info(self):
        """get_routing_info returns current config."""
        config = ModelRoutingConfig(
            routing={
                "planner": ModelRoute(provider="anthropic", model="claude-opus-4-6"),
                "generator": ModelRoute(model="gpt-4o"),
            }
        )
        router = LLMRouter(config, self._make_base_config())
        info = router.get_routing_info()
        assert info["planner"]["model"] == "claude-opus-4-6"
        assert info["planner"]["provider"] == "anthropic"
        assert info["generator"]["model"] == "gpt-4o"
        assert info["generator"]["provider"] == "openai"
