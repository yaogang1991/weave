"""Tests for A2A Agent Card models, Weave card builder, and discovery."""

from __future__ import annotations

import json

from a2a.models import (
    A2ACapabilities,
    A2ACard,
    A2AInterface,
    A2AProvider,
    A2ASkill,
    A2ATaskState,
)
from a2a.agent_card import build_weave_agent_card
from a2a.discovery import (
    AGENT_CARD_PATH,
    AgentCardEndpoint,
    build_well_known_response,
)


# -- Model validation tests --


class TestA2ASkill:
    def test_minimal_skill(self):
        skill = A2ASkill(id="test", name="Test Skill")
        assert skill.id == "test"
        assert skill.tags == []
        assert skill.examples == []

    def test_full_skill(self):
        skill = A2ASkill(
            id="plan",
            name="Plan",
            description="Generate a plan",
            tags=["planning", "dag"],
            examples=["Build an API"],
        )
        assert skill.description == "Generate a plan"
        assert len(skill.tags) == 2

    def test_skill_serialization(self):
        skill = A2ASkill(id="gen", name="Generate")
        data = skill.model_dump()
        assert data["id"] == "gen"
        assert "tags" in data


class TestA2ACapabilities:
    def test_defaults(self):
        caps = A2ACapabilities()
        assert caps.streaming is False
        assert caps.push_notifications is False
        assert caps.extended_agent_card is False

    def test_enabled_capabilities(self):
        caps = A2ACapabilities(streaming=True, push_notifications=True)
        assert caps.streaming is True


class TestA2AInterface:
    def test_defaults(self):
        iface = A2AInterface(url="http://localhost:8080/a2a")
        assert iface.protocol_binding == "json-rpc"
        assert iface.protocol_version == "1.0"

    def test_custom_binding(self):
        iface = A2AInterface(
            url="http://localhost:8080/a2a",
            protocol_binding="grpc",
            protocol_version="2.0",
        )
        assert iface.protocol_binding == "grpc"


class TestA2ACard:
    def test_minimal_card(self):
        card = A2ACard(name="TestAgent", description="A test agent")
        assert card.name == "TestAgent"
        assert card.version == "1.0.0"
        assert card.skills == []
        assert card.supported_interfaces == []

    def test_full_card(self):
        card = A2ACard(
            name="TestAgent",
            description="Full test",
            version="2.0.0",
            provider=A2AProvider(name="Acme"),
            supported_interfaces=[
                A2AInterface(url="http://localhost/a2a")
            ],
            capabilities=A2ACapabilities(streaming=True),
            skills=[
                A2ASkill(id="s1", name="Skill 1"),
            ],
        )
        assert card.provider is not None
        assert card.provider.name == "Acme"
        assert len(card.skills) == 1
        assert card.capabilities.streaming is True

    def test_card_json_round_trip(self):
        card = A2ACard(
            name="Agent",
            description="Desc",
            skills=[A2ASkill(id="s1", name="S1")],
        )
        json_str = card.model_dump_json()
        restored = A2ACard.model_validate_json(json_str)
        assert restored.name == "Agent"
        assert len(restored.skills) == 1

    def test_default_input_output_modes(self):
        card = A2ACard(name="A", description="D")
        assert card.default_input_modes == ["text/plain"]
        assert card.default_output_modes == ["text/plain"]

    def test_metadata_preserved(self):
        card = A2ACard(
            name="A",
            description="D",
            metadata={"custom": "value", "count": 42},
        )
        data = card.model_dump()
        assert data["metadata"]["custom"] == "value"
        assert data["metadata"]["count"] == 42


class TestA2ATaskState:
    def test_all_states(self):
        expected = {
            "submitted", "working", "completed", "failed",
            "canceled", "input_required", "rejected",
        }
        actual = {s.value for s in A2ATaskState}
        assert actual == expected


# -- Weave card builder tests --


class TestBuildWeaveAgentCard:
    def test_default_card(self):
        card = build_weave_agent_card()
        assert card.name == "Weave"
        assert card.version == "0.1.0"
        assert len(card.skills) == 5
        assert card.provider is not None
        assert card.provider.name == "Weave"

    def test_custom_base_url(self):
        card = build_weave_agent_card(base_url="https://weave.example.com")
        assert len(card.supported_interfaces) == 1
        assert card.supported_interfaces[0].url == (
            "https://weave.example.com/a2a"
        )

    def test_custom_version(self):
        card = build_weave_agent_card(version="2.0.0")
        assert card.version == "2.0.0"

    def test_env_var_base_url(self, monkeypatch):
        monkeypatch.setenv("WEAVE_A2A_BASE_URL", "http://custom:9090")
        card = build_weave_agent_card()
        assert card.supported_interfaces[0].url == "http://custom:9090/a2a"

    def test_env_var_version(self, monkeypatch):
        monkeypatch.setenv("WEAVE_VERSION", "3.0.0")
        card = build_weave_agent_card()
        assert card.version == "3.0.0"

    def test_skill_ids(self):
        card = build_weave_agent_card()
        skill_ids = [s.id for s in card.skills]
        assert skill_ids == ["plan", "execute", "generate", "evaluate", "run"]

    def test_metadata_fields(self):
        card = build_weave_agent_card()
        assert card.metadata["framework"] == "weave"
        assert card.metadata["dag_execution"] is True
        assert "planner" in card.metadata["agents"]

    def test_output_modes_include_json(self):
        card = build_weave_agent_card()
        assert "application/json" in card.default_output_modes


# -- Discovery endpoint tests --


class TestAgentCardEndpoint:
    def test_returns_card_dict(self):
        endpoint = AgentCardEndpoint()
        result = endpoint.handle()
        assert result["name"] == "Weave"
        assert "skills" in result

    def test_get_json_is_valid(self):
        endpoint = AgentCardEndpoint()
        json_str = endpoint.get_json()
        data = json.loads(json_str)
        assert data["name"] == "Weave"

    def test_json_caching(self):
        endpoint = AgentCardEndpoint()
        first = endpoint.get_json()
        second = endpoint.get_json()
        assert first is second  # same cached string

    def test_set_card_invalidates_cache(self):
        endpoint = AgentCardEndpoint()
        first = endpoint.get_json()
        custom_card = A2ACard(name="Custom", description="Custom agent")
        endpoint.set_card(custom_card)
        second = endpoint.get_json()
        assert first != second
        assert "Custom" in second

    def test_custom_card_in_constructor(self):
        custom = A2ACard(name="MyAgent", description="Custom")
        endpoint = AgentCardEndpoint(card=custom)
        result = endpoint.handle()
        assert result["name"] == "MyAgent"

    def test_custom_base_url_in_constructor(self):
        endpoint = AgentCardEndpoint(base_url="http://myhost:9999")
        result = endpoint.handle()
        assert result["supported_interfaces"][0]["url"] == (
            "http://myhost:9999/a2a"
        )


class TestBuildWellKnownResponse:
    def test_returns_dict(self):
        result = build_well_known_response()
        assert isinstance(result, dict)
        assert result["name"] == "Weave"

    def test_json_serializable(self):
        result = build_well_known_response()
        json_str = json.dumps(result)
        assert json.loads(json_str)["name"] == "Weave"


class TestAgentCardPath:
    def test_path_is_well_known(self):
        assert AGENT_CARD_PATH == "/.well-known/agent-card.json"
