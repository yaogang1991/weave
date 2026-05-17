"""
CLI DAG Renderer: Real-time command-line visualization using rich.

Provides:
- DAG topology table
- Live status panel
- Execution progress
- Event stream
"""

from __future__ import annotations

import time

from core.models import DAG, ExecutionEvent, NodeStatus


class CLIDAGRenderer:
    """
    Rich-based CLI renderer for DAG execution.

    Usage:
        renderer = CLIDAGRenderer()
        engine.on_event(renderer.handle_event)

        # Before execution:
        renderer.render_dag(dag)

        # After execution:
        renderer.render_summary(dag)
    """

    # Status color mapping for terminal
    STATUS_COLORS = {
        NodeStatus.PENDING: "\033[90m",    # gray
        NodeStatus.RUNNING: "\033[94m",    # blue
        NodeStatus.SUCCESS: "\033[92m",    # green
        NodeStatus.FAILED: "\033[91m",     # red
        NodeStatus.SKIPPED: "\033[93m",    # yellow
        NodeStatus.RETRYING: "\033[95m",   # magenta
    }
    RESET = "\033[0m"
    BOLD = "\033[1m"

    def __init__(self):
        self._node_status: dict[str, str] = {}
        self._node_start_times: dict[str, float] = {}
        self._event_count = 0

    def handle_event(self, event: ExecutionEvent) -> None:
        """Event handler for DAGExecutionEngine.on_event()."""
        self._event_count += 1

        if event.event_type == "started":
            self._node_status[event.node_id] = "running"
            self._node_start_times[event.node_id] = time.time()
            self._print_event("▶️", event.node_id, "STARTED", event.details)

        elif event.event_type == "completed":
            self._node_status[event.node_id] = "success"
            duration = self._get_duration(event.node_id)
            self._print_event("✅", event.node_id, "COMPLETED", event.details, duration)

        elif event.event_type == "failed":
            self._node_status[event.node_id] = "failed"
            self._print_event("❌", event.node_id, "FAILED", event.details)

        elif event.event_type == "retrying":
            self._node_status[event.node_id] = "retrying"
            self._print_event("🔄", event.node_id, "RETRYING", event.details)

        elif event.event_type == "skipped":
            self._node_status[event.node_id] = "skipped"
            self._print_event("⏭️", event.node_id, "SKIPPED", event.details)

    def render_dag(self, dag: DAG) -> None:
        """Print the DAG structure before execution."""
        print()
        hline = "══════════════════════════════════════════════════════════════"
        print(f"{self.BOLD}╔{hline}╗{self.RESET}")
        print(
            f"{self.BOLD}"
            f"║                 DAG Execution Plan                          ║"
            f"{self.RESET}"
        )
        print(f"{self.BOLD}╚{hline}╝{self.RESET}")
        print()

        levels = dag.topological_levels()

        for level_idx, level in enumerate(levels):
            print(f"{self.BOLD}Level {level_idx}:{self.RESET}")
            for nid in level:
                node = dag.nodes[nid]
                agent_color = self._agent_color(node.agent_type)
                print(
                    f"  [{agent_color}{node.agent_type:10}{self.RESET}] "
                    f"{self.BOLD}{nid:15}{self.RESET} {node.task_description[:50]}"
                )
            print()

        if dag.reasoning:
            print(f"{self.BOLD}Reasoning:{self.RESET} {dag.reasoning[:200]}...")
            print()

    def render_live_status(self, dag: DAG) -> None:
        """Print a compact live status of all nodes."""
        print()
        print(f"{self.BOLD}── Live Status ──{self.RESET}")

        for nid, node in dag.nodes.items():
            color = self.STATUS_COLORS.get(node.status, "")
            status_str = f"{color}{node.status.value:8}{self.RESET}"

            duration = ""
            if node.started_at and node.completed_at:
                ms = (node.completed_at - node.started_at).total_seconds() * 1000
                duration = f" ({ms:.0f}ms)"
            elif node.started_at and node.status == NodeStatus.RUNNING:
                ms = (time.time() - node.started_at.timestamp()) * 1000
                duration = f" ({ms:.0f}ms)"

            print(f"  {nid:15} {status_str}{duration}")

        print()

    def render_summary(self, dag: DAG) -> None:
        """Print execution summary."""
        total = len(dag.nodes)
        success = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.SUCCESS)
        failed = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.FAILED)
        skipped = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.SKIPPED)

        print()
        hline = "══════════════════════════════════════════════════════════════"
        print(f"{self.BOLD}╔{hline}╗{self.RESET}")
        print(
            f"{self.BOLD}"
            f"║              Execution Summary                               ║"
            f"{self.RESET}"
        )
        print(f"{self.BOLD}╠{hline}╣{self.RESET}")
        pad = "                                        "
        print(f"{self.BOLD}║{self.RESET}  Total Nodes:   {total:3}{pad}{self.BOLD}║{self.RESET}")
        print(f"{self.BOLD}║{self.RESET}  ✅ Success:    {success:3}{pad}{self.BOLD}║{self.RESET}")
        print(f"{self.BOLD}║{self.RESET}  ❌ Failed:     {failed:3}{pad}{self.BOLD}║{self.RESET}")
        print(f"{self.BOLD}║{self.RESET}  ⏭️ Skipped:    {skipped:3}{pad}{self.BOLD}║{self.RESET}")
        print(f"{self.BOLD}╚{hline}╝{self.RESET}")
        print()

    def _print_event(
        self,
        icon: str,
        node_id: str,
        event_type: str,
        details: dict,
        duration: float | None = None,
    ) -> None:
        """Print a single event line."""
        extra = ""
        if duration is not None:
            extra = f" [{duration:.1f}s]"

        detail_str = ""
        if details:
            # Compact detail representation
            parts = []
            for k, v in details.items():
                if isinstance(v, str) and len(v) > 40:
                    v = v[:37] + "..."
                parts.append(f"{k}={v}")
            detail_str = " | ".join(parts)

        print(f"  {icon} [{node_id:12}] {event_type:10}{extra} {detail_str}")

    def _get_duration(self, node_id: str) -> float | None:
        """Get elapsed time for a node."""
        start = self._node_start_times.get(node_id)
        if start:
            return time.time() - start
        return None

    def _agent_color(self, agent_type: str) -> str:
        """Return a terminal color for an agent type."""
        colors = {
            "planner": "\033[96m",    # cyan
            "generator": "\033[92m",  # green
            "evaluator": "\033[93m",  # yellow
        }
        return colors.get(agent_type, "\033[97m")  # default white
