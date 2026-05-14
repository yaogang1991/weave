"""
Tests for #275: watchdog generator threshold increased for long file writes.

Generator nodes writing 6+ test files need more time before the watchdog
kills them. The heartbeat miss threshold and interval have been increased.
"""
import pytest

from core.config import WatchdogConfig, _DEFAULT_AGENT_WATCHDOG_OVERRIDES


class TestWatchdogGeneratorThreshold:
    def test_generator_interval_is_90s(self):
        """Generator heartbeat interval should be 90s (#275)."""
        gen = _DEFAULT_AGENT_WATCHDOG_OVERRIDES["generator"]
        assert gen.heartbeat_interval_sec == 90.0

    def test_generator_threshold_is_20(self):
        """Generator miss threshold should be 20 (#275).

        With interval=90s and threshold=20, generator nodes get ~30 minutes
        before watchdog kills them — enough for writing 6+ test files.
        """
        gen = _DEFAULT_AGENT_WATCHDOG_OVERRIDES["generator"]
        assert gen.heartbeat_miss_threshold == 20

    def test_generator_effective_timeout(self):
        """Effective kill time for generators: 90s * 20 = 1800s (30 min)."""
        gen = _DEFAULT_AGENT_WATCHDOG_OVERRIDES["generator"]
        effective_timeout = gen.heartbeat_interval_sec * gen.heartbeat_miss_threshold
        assert effective_timeout >= 1800  # 30 minutes

    def test_base_threshold_is_12(self):
        """Default heartbeat_miss_threshold should be 12 (#275)."""
        config = WatchdogConfig()
        assert config.heartbeat_miss_threshold == 12

    def test_settings_for_generator(self):
        """WatchdogConfig.settings_for('generator') returns override values."""
        config = WatchdogConfig()
        interval, threshold = config.settings_for("generator")
        assert interval == 90.0
        assert threshold == 20

    def test_settings_for_planner_uses_defaults(self):
        """WatchdogConfig.settings_for('planner') returns base defaults."""
        config = WatchdogConfig()
        interval, threshold = config.settings_for("planner")
        assert interval == config.heartbeat_interval_sec
        assert threshold == config.heartbeat_miss_threshold

    def test_alert_threshold_for_generator(self):
        """Alert fires at 50% of generator threshold = 10 missed."""
        config = WatchdogConfig()
        alert = config.alert_threshold_for("generator")
        assert alert == 10  # 20 * 0.5
