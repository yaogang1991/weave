"""
LLM Router — Per-agent-type model selection with fallback.

Creates LLMClient instances based on agent type and model routing config.
When no routing is configured, falls back to the default LLMConfig model.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from core.config import LLMConfig, ModelRoute, ModelRoutingConfig, infer_provider
from core.llm_client import LLMClient

logger = logging.getLogger(__name__)


class LLMRouter:
    """Creates LLMClient instances based on agent type and model routing config.

    Caches clients per unique (provider, model) pair for reuse.
    Supports fallback chain when a model is unavailable.
    """

    def __init__(
        self,
        routing_config: ModelRoutingConfig,
        base_llm_config: LLMConfig,
    ):
        self._routing = routing_config
        self._base_config = base_llm_config
        self._clients: dict[str, LLMClient] = {}

    def _make_key(self, provider: str, model: str, **overrides: Any) -> str:
        parts = [provider, model]
        for k in sorted(overrides):
            v = overrides[k]
            if v is not None:
                parts.append(f"{k}={v}")
        return ":".join(parts)

    def _get_or_create(self, provider: str, model: str, **overrides: Any) -> LLMClient:
        """Get cached client or create a new one."""
        key = self._make_key(provider, model, **overrides)
        if key not in self._clients:
            config_data = self._base_config.model_dump()
            config_data["provider"] = provider
            config_data["model"] = model

            # Use provider-specific credentials when routing to a different provider.
            # Only overwrite if the env var is set — preserves custom gateway setups
            # where a single base_url/key handles multiple providers.
            if provider != self._base_config.provider:
                if provider == "openai":
                    _key = os.getenv("OPENAI_API_KEY")
                    if _key:
                        config_data["api_key"] = _key
                    _url = os.getenv("OPENAI_BASE_URL")
                    if _url:
                        config_data["base_url"] = _url
                elif provider == "anthropic":
                    _key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")
                    if _key:
                        config_data["api_key"] = _key
                    _url = os.getenv("ANTHROPIC_BASE_URL")
                    if _url:
                        config_data["base_url"] = _url

            for k, v in overrides.items():
                if v is not None:
                    config_data[k] = v
            config = LLMConfig(**config_data)
            self._clients[key] = LLMClient(config)
            logger.info("Created LLMClient for %s (provider=%s)", model, provider)
        return self._clients[key]

    def _resolve_provider(self, model: str) -> str:
        """Infer provider from model name."""
        return infer_provider(model, default=self._base_config.provider)

    def get_client(self, agent_type: str) -> LLMClient:
        """Get an LLMClient for a specific agent type.

        Args:
            agent_type: Agent type identifier (e.g., "planner", "generator",
                       "evaluator", "orchestrator"). Also accepts custom types
                       registered via agents.yaml.

        Returns:
            LLMClient configured for this agent type. If no routing is
            configured for this agent type, returns the default client.
        """
        route = self._routing.routing.get(agent_type)
        if route is None or (not route.model):
            # No routing configured — use default
            return self._get_or_create(
                self._base_config.provider,
                self._base_config.model,
            )

        provider = route.provider or self._resolve_provider(route.model)
        return self._get_or_create(
            provider,
            route.model,
            temperature=route.temperature,
            max_tokens=route.max_tokens,
        )

    def get_fallback_client(self, failed_model: str) -> LLMClient | None:
        """Get the next available fallback client after a model failure.

        Args:
            failed_model: The model that failed.

        Returns:
            Next LLMClient in the fallback chain, or None if exhausted.
        """
        chain = self._routing.fallback_chain
        # Track seen models to prevent revisiting (handles duplicates in chain)
        seen: set[str] = {failed_model}
        try:
            idx = chain.index(failed_model)
        except ValueError:
            idx = -1

        # Try models after the failed one in the chain, skipping duplicates
        for model in chain[idx + 1:]:
            if model in seen:
                continue
            seen.add(model)
            provider = self._resolve_provider(model)
            try:
                return self._get_or_create(provider, model)
            except Exception as e:
                logger.warning("Fallback model %s failed: %s", model, e)
                continue

        return None

    def get_routing_info(self) -> dict[str, dict[str, str]]:
        """Get current routing configuration as a dict.

        Returns:
            Dict mapping agent_type to {"provider", "model"}.
        """
        info: dict[str, dict[str, str]] = {}
        for agent_type, route in self._routing.routing.items():
            provider = route.provider or self._resolve_provider(route.model)
            info[agent_type] = {"provider": provider, "model": route.model}
        return info
