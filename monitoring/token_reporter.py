"""M5.1 TokenReporter — aggregate trace events into per-Run token summaries."""
from __future__ import annotations

from pydantic import BaseModel, Field

from core.event_models import EventType


class NodeTokenSummary(BaseModel):
    node_id: str = ""
    agent_type: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    tool_call_count: int = 0
    duration_ms: int = 0

    def copy_with(self, **overrides) -> NodeTokenSummary:
        """Return a new instance with specified fields overridden (#804)."""
        return NodeTokenSummary(**{
            "node_id": overrides.get("node_id", self.node_id),
            "agent_type": overrides.get("agent_type", self.agent_type),
            "input_tokens": overrides.get("input_tokens", self.input_tokens),
            "output_tokens": overrides.get("output_tokens", self.output_tokens),
            "tool_call_count": overrides.get("tool_call_count", self.tool_call_count),
            "duration_ms": overrides.get("duration_ms", self.duration_ms),
        })


class TokenSummary(BaseModel):
    run_id: str = ""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_duration_ms: int = 0
    total_nodes: int = 0
    node_summaries: list[NodeTokenSummary] = Field(default_factory=list)


class TokenReporter:
    """Aggregate TRACE events into a TokenSummary for a single Run."""

    def summarize_run(self, events: list) -> TokenSummary:
        nodes: dict[str, NodeTokenSummary] = {}
        run_id = ""
        run_input = 0
        run_output = 0
        run_duration = 0
        run_node_count = 0

        for event in events:
            event_type = event.type
            payload = event.payload if hasattr(event, "payload") else {}

            if event_type == EventType.TRACE_RUN_START:
                run_id = payload.get("run_id", "")

            elif event_type == EventType.TRACE_RUN_END:
                run_duration = payload.get("duration_ms", 0)
                run_input = payload.get("total_input_tokens", 0)
                run_output = payload.get("total_output_tokens", 0)
                run_node_count = payload.get("total_nodes", 0)

            elif event_type == EventType.TRACE_NODE_END:
                nid = payload.get("node_id", "")
                existing = nodes.get(nid, NodeTokenSummary(node_id=nid))
                nodes[nid] = existing.copy_with(
                    agent_type=payload.get("agent_type", existing.agent_type),
                    input_tokens=payload.get("input_tokens", existing.input_tokens),
                    output_tokens=payload.get("output_tokens", existing.output_tokens),
                    duration_ms=payload.get("duration_ms", existing.duration_ms),
                )

            elif event_type == EventType.TRACE_LLM_TURN:
                nid = payload.get("node_id", "")
                existing = nodes.get(nid, NodeTokenSummary(node_id=nid))
                nodes[nid] = existing.copy_with(
                    input_tokens=existing.input_tokens + payload.get("input_tokens", 0),
                    output_tokens=existing.output_tokens + payload.get("output_tokens", 0),
                )

            elif event_type == EventType.TRACE_TOOL_CALL:
                nid = payload.get("node_id", "")
                existing = nodes.get(nid, NodeTokenSummary(node_id=nid))
                nodes[nid] = existing.copy_with(
                    tool_call_count=existing.tool_call_count + 1,
                )

        return TokenSummary(
            run_id=run_id,
            total_input_tokens=run_input,
            total_output_tokens=run_output,
            total_duration_ms=run_duration,
            total_nodes=run_node_count,
            node_summaries=list(nodes.values()),
        )
