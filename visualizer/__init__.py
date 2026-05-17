"""
Harness Visualizer: Real-time DAG execution monitoring.

Provides both Web UI (FastAPI + WebSocket) and CLI (rich) visualization
for the multi-agent orchestration system.
"""

import sys
from pathlib import Path

# Ensure project root is on path when importing visualizer directly
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from visualizer.event_bridge import WebSocketEventBridge  # noqa: E402
from visualizer.cli_renderer import CLIDAGRenderer  # noqa: E402

__all__ = ["WebSocketEventBridge", "CLIDAGRenderer"]
