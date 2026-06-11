"""
Weave UI: Real-time DAG execution monitoring and task management execution monitoring.

Provides both Web UI (FastAPI + WebSocket) and CLI (rich) visualization
for the multi-agent orchestration system.
"""

import sys
from pathlib import Path

# Ensure project root is on path when importing weave_ui directly
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from weave_ui.event_bridge import WebSocketEventBridge  # noqa: E402
from weave_ui.cli_renderer import CLIDAGRenderer  # noqa: E402

__all__ = ["WebSocketEventBridge", "CLIDAGRenderer"]
