"""Tests for #1121: LLMConfig.timeout reads WEAVE_LLM_TIMEOUT.

Previously timeout was hardcoded to 120 with no env-var override, so
third-party LLM APIs needing >120s response time (e.g. GLM-5.x) hit HTTP
timeouts on every planner call. Now it mirrors the api_key / base_url /
max_concurrent_api fields and reads WEAVE_LLM_TIMEOUT.
"""
import pytest

from core.config import LLMConfig


class TestLLMTimeoutEnvVar:
    """#1121: WEAVE_LLM_TIMEOUT overrides the hardcoded default."""

    def test_default_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("WEAVE_LLM_TIMEOUT", raising=False)
        cfg = LLMConfig(api_key="k")
        assert cfg.timeout == 120

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("WEAVE_LLM_TIMEOUT", "300")
        cfg = LLMConfig(api_key="k")
        assert cfg.timeout == 300

    def test_explicit_kwarg_wins(self, monkeypatch):
        """Explicit timeout= still takes precedence (backward compat)."""
        monkeypatch.setenv("WEAVE_LLM_TIMEOUT", "999")
        cfg = LLMConfig(api_key="k", timeout=45)
        assert cfg.timeout == 45

    def test_env_read_at_construction(self, monkeypatch):
        """The env var is read once at construction time, not lazily."""
        monkeypatch.setenv("WEAVE_LLM_TIMEOUT", "200")
        cfg = LLMConfig(api_key="k")
        monkeypatch.setenv("WEAVE_LLM_TIMEOUT", "400")
        assert cfg.timeout == 200  # not 400
