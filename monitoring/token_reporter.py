"""Token usage reporter for M5.1 observability.

Aggregates TRACE events from the session event log into per-Run and
per-Node token summaries.
"""
from __future__ import annotations

from pydantic import BaseModel

from core.event_models import EventType


class NodeTokenSummary(BaseModel):
    node_id: str
    agent_type: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    tool_call_count: int = 0
    duration_ms: int = 0


class TokenSummary(BaseModel):
    run_id: str = ""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_duration_ms: int = 0
    node_summaries: list[NodeTokenSummary] = []


class TokenReporter:
    """Generates token summaries from TRACE events."""

    def summarize_run(self, events: list) -> TokenSummary:
        if not events:
            return TokenSummary()

        run_id = ""
        total_in = 0
        total_out = 0
        total_dur = 0
        node_map: dict[str, dict] = {}

        for ev in events:
            if not hasattr(ev, "type") or not hasattr(ev, "payload"):
                continue

            t = ev.type
            p = ev.payload

            if t == EventType.TRACE_RUN_START:
                run_id = p.get("run_id", "")
            elif t == EventType.TRACE_NODE_END:
                nid = p.get("node_id", "")
                node_map.setdefault(nid, {
                    "node_id": nid,
                    "agent_type": p.get("agent_type", ""),
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "tool_call_count": 0,
                    "duration_ms": 0,
                })
                node_map[nid]["input_tokens"] += p.get("input_tokens", 0)
                node_map[nid]["output_tokens"] += p.get("output_tokens", 0)
                node_map[nid]["duration_ms"] = p.get("duration_ms", 0)
                total_in += p.get("input_tokens", 0)
                total_out += p.get("output_tokens", 0)
            elif t == EventType.TRACE_LLM_TURN:
                nid = p.get("node_id", "")
                node_map.setdefault(nid, {
                    "node_id": nid,
                    "agent_type": "",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "tool_call_count": 0,
                    "duration_ms": 0,
                })
                node_map[nid]["input_tokens"] += p.get("input_tokens", 0)
                node_map[nid]["output_tokens"] += p.get("output_tokens", 0)
                total_in += p.get("input_tokens", 0)
                total_out += p.get("output_tokens", 0)
            elif t == EventType.TRACE_TOOL_CALL:
                nid = p.get("node_id", "")
                node_map.setdefault(nid, {
                    "node_id": nid,
                    "agent_type": "",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "tool_call_count": 0,
                    "duration_ms": 0,
                })
                node_map[nid]["tool_call_count"] += 1
            elif t == EventType.TRACE_RUN_END:
                total_dur = p.get("duration_ms", 0)

        summaries = [
            NodeTokenSummary(**data) for data in node_map.values()
        ]

        return TokenSummary(
            run_id=run_id,
            total_input_tokens=total_in,
            total_output_tokens=total_out,
            total_duration_ms=total_dur,
            node_summaries=summaries,
        )
