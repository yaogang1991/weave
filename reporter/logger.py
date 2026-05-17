"""
Reporter: audit trail, progress tracking, and session reports.
"""

import json
from pathlib import Path

from core.models import EventType
from session.store import SessionStore


class Reporter:
    """
    Generates reports and maintains audit trails.
    All operations are append-only and immutable.
    """

    def __init__(self, session_store: SessionStore, report_path: str = "./data/reports"):
        self.session_store = session_store
        self.report_path = Path(report_path)
        self.report_path.mkdir(parents=True, exist_ok=True)

    def generate_session_report(self, session_id: str) -> str:
        """Generate a markdown report for a completed session."""
        state = self.session_store.restore_state(session_id)
        events = self.session_store.get_events(session_id)

        lines = [
            f"# Session Report: {session_id}",
            f"",
            f"**Status**: {state.status}",
            f"**Stages Completed**: {', '.join(state.stages_completed) or 'None'}",
            f"**Total Events**: {len(events)}",
            f"**Total Tool Calls**: {state.metrics.total_tool_calls}",
            f"**Errors**: {len(state.metrics.errors)}",
            f"",
            "## Event Timeline",
            "",
        ]

        for event in events:
            icon = self._event_icon(event.type)
            lines.append(f"{icon} **{event.type.value}** — {event.timestamp.strftime('%H:%M:%S')}")
            if event.payload:
                # Truncate long payloads
                payload_str = json.dumps(event.payload, default=str)[:200]
                lines.append(f"   `{payload_str}`")
            lines.append("")

        if state.metrics.errors:
            lines.extend([
                "## Errors",
                "",
            ])
            for err in state.metrics.errors:
                lines.append(f"- ❌ {err}")
            lines.append("")

        report = "\n".join(lines)
        
        # Write report
        report_file = self.report_path / f"{session_id}.md"
        report_file.write_text(report, encoding="utf-8")
        
        return str(report_file)

    def _event_icon(self, event_type: EventType) -> str:
        icons = {
            EventType.USER_MESSAGE: "👤",
            EventType.AGENT_MESSAGE: "🤖",
            EventType.AGENT_TOOL_USE: "🔧",
            EventType.AGENT_TOOL_RESULT: "📤",
            EventType.WORKFLOW_STAGE_START: "▶️",
            EventType.WORKFLOW_STAGE_END: "✅",
            EventType.WORKFLOW_STAGE_ERROR: "❌",
            EventType.EVAL_RESULT: "📊",
            EventType.SESSION_START: "🚀",
            EventType.SESSION_END: "🏁",
        }
        return icons.get(event_type, "•")

    def print_progress(self, session_id: str) -> None:
        """Print a concise progress summary to console."""
        state = self.session_store.restore_state(session_id)
        print(f"\n📦 Session {session_id[:8]}... | Status: {state.status}")
        print(f"   Stages: {len(state.stages_completed)} completed")
        print(f"   Tools: {state.metrics.total_tool_calls} calls")
        if state.current_stage:
            print(f"   Current: {state.current_stage}")
        if state.metrics.errors:
            print(f"   ⚠️  {len(state.metrics.errors)} errors")
