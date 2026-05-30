"""Tests for M7.1 Phase 5: LLM client empty choices fix."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.exceptions import InfrastructureError


def _make_openai_client():
    from core.llm_client import LLMClient, LLMConfig
    config = LLMConfig(model="gpt-4", provider="openai", api_key="test-key")
    return LLMClient(config)


def test_empty_choices_raises_infrastructure_error():
    """OpenAI API returning empty choices should raise InfrastructureError."""
    client = _make_openai_client()

    mock_response = MagicMock()
    mock_response.choices = []

    with patch.object(
        client._client.chat.completions, "create", return_value=mock_response
    ):
        with pytest.raises(InfrastructureError, match="empty choices"):
            client._call_openai(
                [{"role": "user", "content": "test"}],
                [],
            )


def test_non_empty_choices_works():
    """Non-empty choices should not raise."""
    client = _make_openai_client()

    mock_choice = MagicMock()
    mock_choice.message.content = "hello"
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    with patch.object(
        client._client.chat.completions, "create", return_value=mock_response
    ):
        result = client._call_openai(
            [{"role": "user", "content": "test"}],
            [],
        )
        assert result["content"] == "hello"
