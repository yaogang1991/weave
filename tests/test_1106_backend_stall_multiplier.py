"""Tests for #1106: backend-specific stall timeout multiplier.

Verifies that:
1. NodeTimeoutConfig.backend_stall_multipliers is applied correctly.
2. stall_timeout_for() multiplies timeout when backend matches.
3. stall_timeout_for() returns unmodified timeout when backend is empty/unknown.
4. NodeExecutor._get_stall_timeout() passes backend through.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from core.config.timeout import NodeTimeoutConfig


class TestBackendStallMultiplier:
    """Tests for backend_stall_multipliers in NodeTimeoutConfig."""

    def test_default_multiplier_for_claude_code(self):
        """Default config includes claude_code with 2.5x multiplier."""
        cfg = NodeTimeoutConfig()
        assert "claude_code" in cfg.backend_stall_multipliers
        assert cfg.backend_stall_multipliers["claude_code"] == 2.5

    def test_stall_timeout_for_with_claude_code_backend(self):
        """stall_timeout_for multiplies by backend multiplier."""
        cfg = NodeTimeoutConfig(
            stall_timeout=120,
            backend_stall_multipliers={"claude_code": 2.5},
        )
        result = cfg.stall_timeout_for("planner", backend="claude_code")
        # 120 * 2.5 = 300
        assert result == 300

    def test_stall_timeout_for_without_backend(self):
        """stall_timeout_for returns base timeout when backend is empty."""
        cfg = NodeTimeoutConfig(stall_timeout=120)
        result = cfg.stall_timeout_for("planner", backend="")
        assert result == 120

    def test_stall_timeout_for_unknown_backend(self):
        """stall_timeout_for returns base timeout for unknown backend."""
        cfg = NodeTimeoutConfig(
            stall_timeout=120,
            backend_stall_multipliers={"claude_code": 2.5},
        )
        result = cfg.stall_timeout_for("planner", backend="builtin")
        assert result == 120

    def test_stall_timeout_for_with_override_and_backend(self):
        """Multiplier applies on top of per-agent-type overrides."""
        cfg = NodeTimeoutConfig(
            stall_timeout=120,
            stall_overrides={"planner": 200},
            backend_stall_multipliers={"claude_code": 2.0},
        )
        result = cfg.stall_timeout_for("planner", backend="claude_code")
        # 200 * 2.0 = 400
        assert result == 400

    def test_stall_timeout_for_dynamic_eval_with_backend(self):
        """Multiplier applies on top of dynamic evaluator scaling."""
        cfg = NodeTimeoutConfig(
            stall_timeout=120,
            backend_stall_multipliers={"claude_code": 2.0},
        )
        # Evaluator with file_count triggers dynamic scaling
        result = cfg.stall_timeout_for(
            "evaluator", file_count=10, backend="claude_code",
        )
        # Dynamic: min(300 + 10*4 + 0*3, 900) = min(340, 900) = 340
        # With multiplier: 340 * 2 = 680
        assert result == 680

    def test_stall_timeout_for_dynamic_gen_with_backend(self):
        """Multiplier applies on top of dynamic generator scaling."""
        cfg = NodeTimeoutConfig(
            stall_timeout=120,
            backend_stall_multipliers={"claude_code": 1.5},
        )
        result = cfg.stall_timeout_for(
            "generator", dep_count=5, feature_count=3, backend="claude_code",
        )
        # Dynamic: min(300 + 5*30 + 3*40, 900) = min(570, 900) = 570
        # With multiplier: int(570 * 1.5) = 855
        assert result == 855

    def test_env_var_override_multiplier(self):
        """WEAVE_BACKEND_STALL_MULTIPLIER_CLAUDE_CODE env var works."""
        with patch.dict(
            os.environ,
            {"WEAVE_BACKEND_STALL_MULTIPLIER_CLAUDE_CODE": "3.0"},
        ):
            cfg = NodeTimeoutConfig()
            assert cfg.backend_stall_multipliers.get("claude_code") == 3.0

    def test_custom_backend_multiplier(self):
        """Custom backends can have their own multipliers."""
        cfg = NodeTimeoutConfig(
            stall_timeout=120,
            backend_stall_multipliers={
                "custom_backend": 4.0,
                "claude_code": 2.5,
            },
        )
        result = cfg.stall_timeout_for(
            "planner", backend="custom_backend",
        )
        assert result == 480  # 120 * 4.0

    def test_multiplier_result_is_int(self):
        """Result is always an int (truncated, not rounded)."""
        cfg = NodeTimeoutConfig(
            stall_timeout=100,
            backend_stall_multipliers={"claude_code": 2.7},
        )
        result = cfg.stall_timeout_for("planner", backend="claude_code")
        assert isinstance(result, int)
        assert result == 270  # int(100 * 2.7) = int(270.0) = 270

    def test_multiplier_of_one_is_noop(self):
        """Multiplier of 1.0 returns the same value."""
        cfg = NodeTimeoutConfig(
            stall_timeout=120,
            backend_stall_multipliers={"claude_code": 1.0},
        )
        result = cfg.stall_timeout_for("planner", backend="claude_code")
        assert result == 120

    def test_empty_backend_stall_multipliers(self):
        """Empty multiplier dict means no multiplier applied."""
        cfg = NodeTimeoutConfig(
            stall_timeout=120,
            backend_stall_multipliers={},
        )
        result = cfg.stall_timeout_for("planner", backend="claude_code")
        assert result == 120


class TestNodeExecutorBackendPassThrough:
    """Tests that NodeExecutor._get_stall_timeout passes backend through."""

    def test_get_stall_timeout_passes_backend(self):
        """_get_stall_timeout passes backend to stall_timeout_for."""
        from core.node_executor import NodeExecutor

        cfg = NodeTimeoutConfig(
            stall_timeout=120,
            backend_stall_multipliers={"claude_code": 2.5},
        )
        executor = NodeExecutor.__new__(NodeExecutor)
        executor._node_timeout_config = cfg
        executor._agent_registry = None
        executor._watchdog = MagicMock()

        # With claude_code backend
        result = executor._get_stall_timeout(
            "planner", node=None, backend="claude_code",
        )
        assert result == 300  # 120 * 2.5

        # Without backend
        result = executor._get_stall_timeout(
            "planner", node=None, backend="",
        )
        assert result == 120

    def test_get_stall_timeout_no_config_no_backend(self):
        """_get_stall_timeout works without NodeTimeoutConfig."""
        from core.node_executor import NodeExecutor

        executor = NodeExecutor.__new__(NodeExecutor)
        executor._node_timeout_config = None
        executor._agent_registry = None
        executor._watchdog = MagicMock()
        executor._watchdog.get_heartbeat_settings.return_value = (30, 8)

        result = executor._get_stall_timeout(
            "planner", node=None, backend="claude_code",
        )
        # Falls back to heartbeat-based: 30 * 8 = 240
        assert result == 240
