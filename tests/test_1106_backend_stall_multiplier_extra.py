"""Extended tests for #1106: backend-specific stall timeout multiplier.

These tests supplement tests/test_1106_backend_stall_multiplier.py by
covering additional edge cases, integration scenarios, and regression
tests for the original bug (false stall kills on slow third-party APIs).
"""

import os
from unittest.mock import MagicMock, patch

from core.config.timeout import NodeTimeoutConfig


# ------------------------------------------------------------------
# Edge cases for stall_timeout_for()
# ------------------------------------------------------------------


class TestStallTimeoutForEdgeCases:
    """Edge-case tests for stall_timeout_for with backend multiplier."""

    def test_backend_none_treated_as_empty(self):
        """backend=None should not match any multiplier (no crash)."""
        cfg = NodeTimeoutConfig(
            stall_timeout=120,
            backend_stall_multipliers={"claude_code": 2.5},
        )
        # backend defaults to "" — passing None explicitly is not
        # the intended API, but should not crash.
        result = cfg.stall_timeout_for("planner", backend=None)
        # None is falsy, so the multiplier branch is skipped.
        assert result == 120

    def test_zero_multiplier_rejected(self):
        """A multiplier of 0.0 is rejected — it disables stall detection (#1131)."""
        import pytest
        with pytest.raises(Exception):
            NodeTimeoutConfig(
                stall_timeout=120,
                backend_stall_multipliers={"claude_code": 0.0},
            )

    def test_negative_multiplier_rejected(self):
        """A negative multiplier is rejected — it produces a broken negative timeout (#1131)."""
        import pytest
        with pytest.raises(Exception):
            NodeTimeoutConfig(
                stall_timeout=120,
                backend_stall_multipliers={"claude_code": -1.0},
            )

    def test_very_large_multiplier(self):
        """Very large multiplier produces proportionally large timeout."""
        cfg = NodeTimeoutConfig(
            stall_timeout=120,
            backend_stall_multipliers={"claude_code": 100.0},
        )
        result = cfg.stall_timeout_for("planner", backend="claude_code")
        assert result == 12000  # 120 * 100

    def test_fractional_multiplier_rounds(self):
        """Fractional results are rounded to nearest int, not truncated (#1131)."""
        cfg = NodeTimeoutConfig(
            stall_timeout=100,
            backend_stall_multipliers={"claude_code": 1.999},
        )
        result = cfg.stall_timeout_for("planner", backend="claude_code")
        # round(100 * 1.999) = round(199.9) = 200
        assert result == 200

    def test_multiplier_with_dynamic_cap(self):
        """Multiplier applies after dynamic cap is enforced."""
        cfg = NodeTimeoutConfig(
            stall_timeout=120,
            backend_stall_multipliers={"claude_code": 2.0},
        )
        # Evaluator with very high file_count hits the cap (900)
        result = cfg.stall_timeout_for(
            "evaluator", file_count=500, backend="claude_code",
        )
        # Dynamic: min(300 + 500*4, 900) = min(2300, 900) = 900
        # With multiplier: int(900 * 2.0) = 1800
        assert result == 1800

    def test_backend_case_sensitive(self):
        """Backend name matching is case-sensitive."""
        cfg = NodeTimeoutConfig(
            stall_timeout=120,
            backend_stall_multipliers={"claude_code": 2.5},
        )
        # "Claude_Code" should NOT match "claude_code"
        result = cfg.stall_timeout_for(
            "planner", backend="Claude_Code",
        )
        assert result == 120  # No multiplier applied

    def test_backend_with_whitespace(self):
        """Backend name with whitespace does not match."""
        cfg = NodeTimeoutConfig(
            stall_timeout=120,
            backend_stall_multipliers={"claude_code": 2.5},
        )
        result = cfg.stall_timeout_for(
            "planner", backend=" claude_code",
        )
        assert result == 120

    def test_multiple_backends_independent(self):
        """Each backend gets its own multiplier independently."""
        cfg = NodeTimeoutConfig(
            stall_timeout=100,
            backend_stall_multipliers={
                "claude_code": 2.0,
                "openai": 3.0,
                "custom": 1.5,
            },
        )
        assert cfg.stall_timeout_for(
            "planner", backend="claude_code",
        ) == 200
        assert cfg.stall_timeout_for(
            "planner", backend="openai",
        ) == 300
        assert cfg.stall_timeout_for(
            "planner", backend="custom",
        ) == 150

    def test_stall_override_with_dynamic_and_backend(self):
        """All three layers combine: override > dynamic, then * multiplier."""
        cfg = NodeTimeoutConfig(
            stall_timeout=60,
            stall_overrides={"evaluator": 500},
            backend_stall_multipliers={"claude_code": 2.0},
        )
        # evaluator has override=500, dynamic with file_count=10:
        # dynamic = min(300 + 10*4, 900) = 340
        # configured = max(500, 340) = 500 (override wins)
        # result = int(500 * 2.0) = 1000
        result = cfg.stall_timeout_for(
            "evaluator", file_count=10, backend="claude_code",
        )
        assert result == 1000

    def test_generator_dynamic_exceeds_cap_with_multiplier(self):
        """Generator dynamic hits cap, then multiplier amplifies it."""
        cfg = NodeTimeoutConfig(
            stall_timeout=120,
            backend_stall_multipliers={"claude_code": 3.0},
        )
        # Generator with high dep/feature count hits cap (900)
        result = cfg.stall_timeout_for(
            "generator",
            dep_count=50,
            feature_count=30,
            backend="claude_code",
        )
        # Dynamic: min(300 + 50*30 + 30*40, 900)
        #        = min(300 + 1500 + 1200, 900) = min(3000, 900) = 900
        # With multiplier: int(900 * 3.0) = 2700
        assert result == 2700


# ------------------------------------------------------------------
# Regression tests for the original bug
# ------------------------------------------------------------------


class TestOriginalBugRegression:
    """Regression tests that reproduce the original #1106 bug scenario.

    The original bug: when using claude_code backend with a slow
    third-party LLM API (GLM-5.1 via Anthropic-compatible proxy),
    the default 120s stall timeout was too short. The API would not
    produce streaming events for extended periods, causing false
    stall kills. The fix adds a 2.5x multiplier for claude_code.
    """

    def test_original_bug_claude_code_timeout_too_short(self):
        """Without fix, claude_code gets only 120s stall (too short)."""
        # Simulate the pre-fix behavior: no backend multiplier
        cfg = NodeTimeoutConfig(
            stall_timeout=120,
            backend_stall_multipliers={},  # Empty = pre-fix behavior
        )
        result = cfg.stall_timeout_for("planner", backend="claude_code")
        assert result == 120  # This was the bug: too short for slow APIs

    def test_fix_provides_extended_timeout_for_claude_code(self):
        """With fix, claude_code gets 300s stall (120 * 2.5)."""
        cfg = NodeTimeoutConfig(stall_timeout=120)
        result = cfg.stall_timeout_for("planner", backend="claude_code")
        assert result == 300  # Fixed: 120 * 2.5 = 300s

    def test_fix_does_not_affect_builtin_backend(self):
        """Builtin backend still gets the original 120s timeout."""
        cfg = NodeTimeoutConfig(stall_timeout=120)
        result = cfg.stall_timeout_for("planner", backend="builtin")
        assert result == 120  # Unchanged for builtin

    def test_fix_does_not_affect_empty_backend(self):
        """No backend specified = no multiplier (backward compat)."""
        cfg = NodeTimeoutConfig(stall_timeout=120)
        result = cfg.stall_timeout_for("planner", backend="")
        assert result == 120

    def test_slow_api_scenario_generator(self):
        """Generator with claude_code gets adequate stall timeout.

        A generator with 10 dependencies and 5 features should get
        a generous stall timeout when using claude_code backend.
        """
        cfg = NodeTimeoutConfig(stall_timeout=120)
        result = cfg.stall_timeout_for(
            "generator",
            dep_count=10,
            feature_count=5,
            backend="claude_code",
        )
        # Dynamic: min(300 + 10*30 + 5*40, 900) = min(800, 900) = 800
        # max(120, 800) = 800
        # With multiplier: int(800 * 2.5) = 2000
        assert result == 2000
        # 2000s = 33+ minutes, adequate for slow API

    def test_slow_api_scenario_evaluator(self):
        """Evaluator with claude_code gets adequate stall timeout."""
        cfg = NodeTimeoutConfig(stall_timeout=120)
        result = cfg.stall_timeout_for(
            "evaluator",
            file_count=20,
            test_count=10,
            backend="claude_code",
        )
        # Dynamic: min(300 + 20*4 + 10*3, 900) = min(410, 900) = 410
        # max(120, 410) = 410
        # With multiplier: int(410 * 2.5) = 1025
        assert result == 1025


# ------------------------------------------------------------------
# NodeExecutor integration tests
# ------------------------------------------------------------------


class TestNodeExecutorBackendResolution:
    """Tests for backend name resolution in _execute_with_timeout."""

    def test_backend_name_from_node_backend_field(self):
        """Backend name is resolved from node.backend when set."""
        from core.node_executor import NodeExecutor

        cfg = NodeTimeoutConfig(
            stall_timeout=120,
            backend_stall_multipliers={"claude_code": 2.5},
        )
        executor = NodeExecutor.__new__(NodeExecutor)
        executor._node_timeout_config = cfg
        executor._agent_registry = None
        executor._watchdog = MagicMock()

        # Simulate a node with backend="claude_code"
        node = MagicMock()
        node.backend = "claude_code"
        node.task_description = "test task"

        result = executor._get_stall_timeout(
            "planner", node=node, backend="claude_code",
        )
        assert result == 300  # 120 * 2.5

    def test_backend_name_falls_back_to_default(self):
        """When node.backend is None, default_agent_backend is used."""
        from core.node_executor import NodeExecutor

        cfg = NodeTimeoutConfig(
            stall_timeout=120,
            backend_stall_multipliers={"claude_code": 2.5},
        )
        executor = NodeExecutor.__new__(NodeExecutor)
        executor._node_timeout_config = cfg
        executor._agent_registry = None
        executor._watchdog = MagicMock()

        # Backend resolved as default_agent_backend = "claude_code"
        result = executor._get_stall_timeout(
            "planner", node=None, backend="claude_code",
        )
        assert result == 300

    def test_no_backend_registry_gives_empty_backend(self):
        """When _backend_registry is None, backend is empty string."""
        from core.node_executor import NodeExecutor

        cfg = NodeTimeoutConfig(
            stall_timeout=120,
            backend_stall_multipliers={"claude_code": 2.5},
        )
        executor = NodeExecutor.__new__(NodeExecutor)
        executor._node_timeout_config = cfg
        executor._agent_registry = None
        executor._watchdog = MagicMock()

        # No backend registry → backend="" → no multiplier
        result = executor._get_stall_timeout(
            "planner", node=None, backend="",
        )
        assert result == 120

    def test_get_stall_timeout_with_node_complexity_and_backend(self):
        """_get_stall_timeout extracts complexity from node + backend."""
        from core.node_executor import NodeExecutor

        cfg = NodeTimeoutConfig(
            stall_timeout=120,
            backend_stall_multipliers={"claude_code": 2.0},
        )
        executor = NodeExecutor.__new__(NodeExecutor)
        executor._node_timeout_config = cfg
        executor._agent_registry = None
        executor._watchdog = MagicMock()

        # Create a mock node with complexity info
        node = MagicMock()
        node.task_description = "Implement 5 features"
        # extract_node_complexity returns (file_count, test_count, dep_count)
        # These are imported locally inside _get_stall_timeout, so patch
        # at the source module.
        with patch(
            "core.node_utils.extract_node_complexity",
            return_value=(8, 3, 0),
        ):
            with patch(
                "core.node_utils.estimate_feature_count",
                return_value=5,
            ):
                result = executor._get_stall_timeout(
                    "evaluator", node=node, backend="claude_code",
                )
        # Dynamic: min(300 + 8*4 + 3*3, 900) = min(341, 900) = 341
        # max(120, 341) = 341
        # With multiplier: int(341 * 2.0) = 682
        assert result == 682


# ------------------------------------------------------------------
# Environment variable tests
# ------------------------------------------------------------------


class TestBackendStallMultiplierEnvVars:
    """Tests for environment variable configuration of multipliers."""

    def test_env_var_set_to_high_value(self):
        """High multiplier via env var for very slow APIs."""
        with patch.dict(
            os.environ,
            {"WEAVE_BACKEND_STALL_MULTIPLIER_CLAUDE_CODE": "5.0"},
        ):
            cfg = NodeTimeoutConfig()
            assert cfg.backend_stall_multipliers["claude_code"] == 5.0
            result = cfg.stall_timeout_for(
                "planner", backend="claude_code",
            )
            # Default stall_timeout=120, 120 * 5.0 = 600
            assert result == 600

    def test_env_var_set_to_one(self):
        """Multiplier of 1.0 via env var effectively disables multiplier."""
        with patch.dict(
            os.environ,
            {"WEAVE_BACKEND_STALL_MULTIPLIER_CLAUDE_CODE": "1.0"},
        ):
            cfg = NodeTimeoutConfig()
            result = cfg.stall_timeout_for(
                "planner", backend="claude_code",
            )
            assert result == cfg.stall_timeout

    def test_env_var_with_fractional_value(self):
        """Fractional env var value works correctly."""
        with patch.dict(
            os.environ,
            {"WEAVE_BACKEND_STALL_MULTIPLIER_CLAUDE_CODE": "1.5"},
        ):
            cfg = NodeTimeoutConfig()
            assert cfg.backend_stall_multipliers["claude_code"] == 1.5

    def test_default_without_env_var(self):
        """Default multiplier is 2.5 when env var is not set."""
        # Ensure env var is not set
        env = dict(os.environ)
        env.pop("WEAVE_BACKEND_STALL_MULTIPLIER_CLAUDE_CODE", None)
        with patch.dict(os.environ, env, clear=True):
            cfg = NodeTimeoutConfig()
            assert cfg.backend_stall_multipliers["claude_code"] == 2.5

    def test_bad_env_var_rejected_at_config_load(self):
        """A bad env-var multiplier (< 1.0) is rejected at config load (#1131).

        The field_validator must run on the default_factory output too.
        validate_default=True ensures WEAVE_BACKEND_STALL_MULTIPLIER_CLAUDE_CODE
        set to 0.5 / 0 / -1 raises at construction rather than silently
        producing a multiplier that disables or inverts stall detection.
        """
        import pytest
        for bad in ("0.5", "0", "-1.0"):
            with patch.dict(
                os.environ,
                {"WEAVE_BACKEND_STALL_MULTIPLIER_CLAUDE_CODE": bad},
            ):
                with pytest.raises(Exception):
                    NodeTimeoutConfig()


# ------------------------------------------------------------------
# Serialization / deserialization tests
# ------------------------------------------------------------------


class TestBackendStallMultiplierSerialization:
    """Tests for Pydantic model serialization of backend_stall_multipliers."""

    def test_config_serialization_round_trip(self):
        """Config with custom multipliers survives serialization."""
        original = NodeTimeoutConfig(
            stall_timeout=120,
            backend_stall_multipliers={
                "claude_code": 2.5,
                "custom": 3.0,
            },
        )
        data = original.model_dump()
        restored = NodeTimeoutConfig(**data)
        assert restored.backend_stall_multipliers == {
            "claude_code": 2.5,
            "custom": 3.0,
        }
        # Verify behavior is preserved
        assert restored.stall_timeout_for(
            "planner", backend="claude_code",
        ) == 300
        assert restored.stall_timeout_for(
            "planner", backend="custom",
        ) == 360

    def test_json_serialization_round_trip(self):
        """Config survives JSON serialization round-trip."""
        original = NodeTimeoutConfig(
            stall_timeout=120,
            backend_stall_multipliers={"claude_code": 2.5},
        )
        json_str = original.model_dump_json()
        restored = NodeTimeoutConfig.model_validate_json(json_str)
        assert restored.backend_stall_multipliers == {
            "claude_code": 2.5,
        }
        assert restored.stall_timeout_for(
            "planner", backend="claude_code",
        ) == 300


# ------------------------------------------------------------------
# F3: wall-clock kill must not fire before stall (#1131)
# ------------------------------------------------------------------


class TestWallClockFlooredAtStallTimeout:
    """#1131: the #1079 wall-clock hard kill is floored at the
    (backend-scaled) stall timeout so it cannot fire first.

    A claude_code node gets a stall timeout that meets or exceeds the
    default wall-clock timeout; without the floor in
    NodeExecutor._execute_with_timeout the raised stall would be defeated
    by the earlier wall-clock kill.  These tests verify the numerical
    relationship that makes the floor necessary.
    """

    def test_claude_code_planner_stall_meets_wall_clock(self):
        """claude_code planner stall (120*2.5=300) meets wall-clock (300)."""
        cfg = NodeTimeoutConfig()
        wall_clock = cfg.timeout_for("planner")  # default 300s
        stall = cfg.stall_timeout_for("planner", backend="claude_code")
        # 300 == 300 — floor ensures wall-clock never fires strictly before stall
        assert stall >= wall_clock

    def test_claude_code_generator_stall_far_exceeds_wall_clock(self):
        """generator: claude_code stall (dynamic * 2.5) >> wall-clock 600s.

        Without the #1131 floor, the #1079 wall-clock kill (600s) would fire
        long before the raised stall timeout (2000s) — defeating the PR.
        """
        cfg = NodeTimeoutConfig()
        wall_clock = cfg.timeout_for("generator")  # 600s override
        stall = cfg.stall_timeout_for(
            "generator",
            dep_count=10,
            feature_count=5,
            backend="claude_code",
        )  # 800 * 2.5 = 2000s
        assert stall > wall_clock

    def test_builtin_backend_no_multiplier_inflation(self):
        """builtin backend gets no stall inflation (no 2.5x)."""
        cfg = NodeTimeoutConfig()
        stall_builtin = cfg.stall_timeout_for(
            "planner", backend="builtin",
        )
        stall_none = cfg.stall_timeout_for("planner", backend="")
        assert stall_builtin == stall_none == cfg.stall_timeout
