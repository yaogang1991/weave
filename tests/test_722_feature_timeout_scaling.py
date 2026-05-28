"""Tests for #722: scale stall timeout by estimated feature count.

Verifies:
1. estimate_feature_count detects enumerated and comma-separated features
2. stall_timeout_for scales with feature_count for generators
3. Feature-based scaling is capped
"""
from core.config import GeneratorStallScaleConfig, NodeTimeoutConfig
from core.node_utils import estimate_feature_count


class TestEstimateFeatureCount:
    """Verify feature count estimation heuristics."""

    def test_enumerated_items(self):
        task = (
            "1) apply_patch function, 2) create_patch function, "
            "3) merge_changes function, 4) rollback function"
        )
        assert estimate_feature_count(task) == 4

    def test_comma_separated_implement(self):
        task = "implement apply_patch, create_patch, and merge_changes"
        assert estimate_feature_count(task) >= 3

    def test_simple_task_returns_small(self):
        task = "implement a simple hello world function"
        assert estimate_feature_count(task) >= 1

    def test_empty_task(self):
        assert estimate_feature_count("") == 0
        assert estimate_feature_count(None) == 0

    def test_few_items_not_counted(self):
        task = "1) do something, 2) do another thing"
        assert estimate_feature_count(task) == 0  # < 3 enumerated


class TestFeatureTimeoutScaling:
    """Verify stall_timeout_for scales with feature_count (#722)."""

    def test_generator_timeout_scales_with_features(self):
        cfg = NodeTimeoutConfig()
        # With default gen_stall_scale: base=180, per_feature=60
        base_timeout = cfg.stall_timeout_for(
            "generator", dep_count=0, feature_count=0,
        )
        scaled_timeout = cfg.stall_timeout_for(
            "generator", dep_count=0, feature_count=8,
        )
        # Should increase: 180 + 8 * 60 = 660 (vs stall_overrides floor 240)
        assert scaled_timeout > base_timeout
        assert scaled_timeout == 660

    def test_generator_timeout_capped(self):
        cfg = NodeTimeoutConfig()
        timeout = cfg.stall_timeout_for(
            "generator", dep_count=0, feature_count=100,
        )
        # Should be capped at gen_stall_scale.cap (default 900)
        assert timeout <= 900

    def test_combined_dep_and_feature_scaling(self):
        cfg = NodeTimeoutConfig()
        timeout = cfg.stall_timeout_for(
            "generator", dep_count=3, feature_count=5,
        )
        # 180 + 3*60 + 5*60 = 180 + 180 + 300 = 660
        assert timeout == 660

    def test_feature_scaling_only_for_generators(self):
        cfg = NodeTimeoutConfig()
        timeout = cfg.stall_timeout_for(
            "evaluator", feature_count=10,
        )
        # Evaluators don't use feature scaling
        assert timeout == cfg.stall_timeout

    def test_per_feature_configurable(self):
        """per_feature can be set via constructor."""
        scale = GeneratorStallScaleConfig(per_feature=60)
        assert scale.per_feature == 60

        cfg = NodeTimeoutConfig()
        cfg.gen_stall_scale = scale
        timeout = cfg.stall_timeout_for(
            "generator", feature_count=10,
        )
        # 180 + 10*60 = 780, capped at 900
        assert timeout == 780
