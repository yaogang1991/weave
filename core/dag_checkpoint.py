"""
DAG execution state persistence for crash recovery (#455).

Extracted from DAGExecutionEngine for maintainability (#516).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class CheckpointManager:
    """Manages per-session checkpoint files for DAG execution state.

    Each session gets a JSONL file under the checkpoint directory.
    Completed nodes are appended as lines; on crash recovery they are
    loaded so the engine can skip already-completed work.
    """

    def __init__(self, checkpoint_dir: str, session_id: str | None) -> None:
        self._checkpoint_dir = Path(checkpoint_dir)
        self._session_id = session_id
        self._dir_created = False

    def _file_path(self) -> Path:
        """Return checkpoint file path for current session."""
        if not self._dir_created:
            self._checkpoint_dir.mkdir(parents=True, exist_ok=True)
            self._dir_created = True
        return self._checkpoint_dir / f"{self._session_id}.jsonl"

    def persist_node_completion(
        self, node_id: str, result: dict[str, Any] | None,
    ) -> None:
        """Append node completion record to checkpoint file (#455)."""
        if not self._session_id:
            return
        path = self._file_path()
        entry: dict[str, Any] = {
            "node_id": node_id,
            "status": "completed",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if result:
            entry["result"] = result
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError as exc:
            logger.warning(
                "Failed to persist node %s checkpoint: %s", node_id, exc,
            )

    def load_completed_nodes(self) -> dict[str, dict[str, Any] | None]:
        """Load completed node IDs and their results from checkpoint (#455).

        Returns dict mapping node_id to its persisted result dict (or None).
        Corrupt entries are skipped.
        """
        if not self._session_id:
            return {}
        path = self._file_path()
        if not path.exists():
            return {}
        completed: dict[str, dict[str, Any] | None] = {}
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("status") == "completed":
                        completed[entry["node_id"]] = entry.get("result")
                except (json.JSONDecodeError, KeyError):
                    continue
        except OSError as exc:
            logger.warning(
                "Failed to load checkpoint for session %s: %s",
                self._session_id, exc,
            )
        return completed

    def cleanup(self) -> None:
        """Remove checkpoint file after successful DAG completion (#455)."""
        if not self._session_id:
            return
        path = self._file_path()
        if path.exists():
            try:
                path.unlink()
                logger.info(
                    "Cleaned up checkpoint for session %s", self._session_id,
                )
            except OSError as exc:
                logger.warning(
                    "Failed to cleanup checkpoint for session %s: %s",
                    self._session_id, exc,
                )
