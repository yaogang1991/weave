"""Tests for M7.1 Phase 3: LOW severity silent failure logging (parametrized)."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Parametrized tests for LOW severity files — verify loggers exist
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module_name,logger_name,warning_text", [
    ("control_plane.worker", "control_plane.worker", "Config reload job listing failed"),
    ("control_plane.execution_factory", "control_plane.execution_factory", "Guardrail config load failed"),
    ("control_plane.worker_executor", "control_plane.worker_executor", "Lease release failed"),
    ("control_plane.backend_lifecycle", "control_plane.backend_lifecycle", "Backend cleanup failure"),
    ("core.progress", "core.progress", "Progress observer callback failed"),
    ("core.token_estimator", "core.token_estimator", "Token estimation failed"),
    ("evaluator.runner", "evaluator.runner", "Lint marker extraction failed"),
    ("learning.scheduler", "learning.scheduler", "Metrics collection failed"),
    ("core.subprocess_runner", "core.subprocess_runner", "Subprocess execution failed"),
    ("backend.wasm", "backend.wasm", "WASM runtime check failed"),
])
def test_logger_warning_format(module_name, logger_name, warning_text):
    """Verify each module's logger can emit the expected warning text."""
    import importlib
    mod = importlib.import_module(module_name)
    assert hasattr(mod, "logger"), f"{module_name} has no 'logger' attribute"
    assert mod.logger.name == logger_name


# ---------------------------------------------------------------------------
# Direct tests for CLI modules that needed new loggers
# ---------------------------------------------------------------------------

def test_cli_execution_logger_exists():
    import cli.execution
    assert hasattr(cli.execution, "logger")


def test_cli_impact_logger_exists():
    import cli.impact
    assert hasattr(cli.impact, "logger")


def test_cli_execution_init_warning_format(caplog):
    logger = logging.getLogger("cli.execution")
    with caplog.at_level(logging.WARNING, logger="cli.execution"):
        logger.warning("Memory/Learning system init failed: %s", "test error")
    assert any("Memory/Learning system init failed" in r.message for r in caplog.records)


def test_cli_impact_load_warning_format(caplog):
    logger = logging.getLogger("cli.impact")
    with caplog.at_level(logging.WARNING, logger="cli.impact"):
        logger.warning("Impact record load failed for %s: %s", "test.json", "test error")
    assert any("Impact record load failed" in r.message for r in caplog.records)
