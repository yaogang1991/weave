"""Shared test fixtures and async-test compatibility hooks."""

import asyncio
import inspect
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure project root is on sys.path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import LLMConfig
from core.models import ToolResult
from session.store import SessionStore


def pytest_configure(config: pytest.Config) -> None:
    """Register local markers to avoid strict marker warnings."""
    config.addinivalue_line("markers", "asyncio: mark test as asyncio-compatible")


def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    """Run async test functions when external async plugins are unavailable."""
    test_func = pyfuncitem.obj
    if inspect.iscoroutinefunction(test_func):
        fixture_names = pyfuncitem._fixtureinfo.argnames
        kwargs = {name: pyfuncitem.funcargs[name] for name in fixture_names}
        asyncio.run(test_func(**kwargs))
        return True
    return None


@pytest.fixture
def tmp_store(tmp_path):
    """SessionStore backed by a temporary directory."""
    return SessionStore(str(tmp_path / "events"))


@pytest.fixture
def llm_config():
    return LLMConfig(api_key="test-key", model="test-model")


@pytest.fixture
def mock_llm_client():
    """Mock LLMClient that returns a configurable sequence of responses."""
    client = MagicMock()
    client.call = MagicMock(return_value={
        "role": "assistant",
        "content": "Task completed",
    })
    return client


@pytest.fixture
def mock_tool_executor():
    """Mock tool executor that returns success."""
    executor = MagicMock()
    executor.execute = MagicMock(return_value=ToolResult(
        tool_call_id="test-id",
        success=True,
        output="Tool output",
    ))
    return executor
