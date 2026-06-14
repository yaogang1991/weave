"""Tests for text-artifact extraction failure diagnostics (#1137 track 3b).

Each early-return path should emit an INFO log explaining WHY extraction
returned nothing, so debugging zero_output_artifacts doesn't require
reading source.
"""
from __future__ import annotations

import logging

from agent.backends.text_artifact_extractor import extract_artifacts_from_text


class TestExtractionDiagnostics:
    def test_logs_when_text_too_short(self, tmp_path, caplog):
        workspace = str(tmp_path)
        with caplog.at_level(logging.INFO, logger="agent.backends.text_artifact_extractor"):
            result = extract_artifacts_from_text("short", workspace)
        assert result == []
        assert any(
            "below minimum length" in r.message for r in caplog.records
        ), [r.message for r in caplog.records]

    def test_logs_when_non_generator(self, tmp_path, caplog):
        workspace = str(tmp_path)
        text = "```python\nprint('x')\n```\n" * 30  # long enough
        with caplog.at_level(logging.INFO, logger="agent.backends.text_artifact_extractor"):
            result = extract_artifacts_from_text(
                text, workspace, node_context={"agent_type": "planner"},
            )
        assert result == []
        assert any(
            "non-generator node" in r.message for r in caplog.records
        ), [r.message for r in caplog.records]

    def test_logs_when_all_blocks_filtered(self, tmp_path, caplog):
        workspace = str(tmp_path)
        # All blocks below the 20-char threshold.
        text = "```python\nx\n```\n" + "padding\n" * 40
        with caplog.at_level(logging.INFO, logger="agent.backends.text_artifact_extractor"):
            result = extract_artifacts_from_text(text, workspace)
        assert result == []
        assert any(
            "below" in r.message and "threshold" in r.message
            for r in caplog.records
        ), [r.message for r in caplog.records]

    def test_no_diagnostic_log_on_success(self, tmp_path, caplog):
        """A successful extraction must not log a skip reason."""
        workspace = str(tmp_path)
        text = (
            "```python\n"
            "# file: app.py\n"
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "```\n"
        )
        text = text + "\n" * 200
        with caplog.at_level(logging.INFO, logger="agent.backends.text_artifact_extractor"):
            result = extract_artifacts_from_text(
                text, workspace, node_context={"agent_type": "generator"},
            )
        assert result == ["app.py"]
        assert not any(
            "skipped" in r.message for r in caplog.records
        ), [r.message for r in caplog.records]
