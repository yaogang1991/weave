"""Unit tests for M4.2 BudgetManager."""

import threading

import pytest

from core.config import BudgetConfig
from core.budget_manager import BudgetManager


class TestBudgetManager:
    def test_unlimited_budget_always_passes_check(self):
        bm = BudgetManager(BudgetConfig(total_tokens=0))
        assert bm.config.is_unlimited
        assert bm.check() is True
        bm.record_usage(999_999_999, 999_999_999)
        assert bm.check() is True

    def test_check_passes_when_under_budget(self):
        bm = BudgetManager(BudgetConfig(total_tokens=1000))
        bm.record_usage(100, 200)
        assert bm.check() is True

    def test_check_fails_when_budget_exhausted(self):
        bm = BudgetManager(BudgetConfig(total_tokens=1000))
        bm.record_usage(600, 500)
        assert bm.check() is False

    def test_check_fails_at_exact_limit(self):
        bm = BudgetManager(BudgetConfig(total_tokens=1000))
        bm.record_usage(600, 400)
        assert bm.check() is False

    def test_remaining_tokens_decreases(self):
        bm = BudgetManager(BudgetConfig(total_tokens=1000))
        assert bm.remaining_tokens == 1000
        bm.record_usage(300, 200)
        assert bm.remaining_tokens == 500

    def test_remaining_tokens_unlimited(self):
        bm = BudgetManager(BudgetConfig(total_tokens=0))
        assert bm.remaining_tokens == -1

    def test_usage_fraction(self):
        bm = BudgetManager(BudgetConfig(total_tokens=1000))
        assert bm.usage_fraction == 0.0
        bm.record_usage(300, 200)
        assert bm.usage_fraction == 0.5

    def test_usage_fraction_unlimited(self):
        bm = BudgetManager(BudgetConfig(total_tokens=0))
        assert bm.usage_fraction == 0.0

    def test_warning_threshold_one_shot(self):
        bm = BudgetManager(BudgetConfig(total_tokens=1000, warning_threshold=0.8))
        bm.record_usage(400, 300)  # 700/1000 = 0.7
        assert bm.check_warning() is False
        bm.record_usage(50, 50)  # 800/1000 = 0.8
        assert bm.check_warning() is True
        assert bm.check_warning() is False  # One-shot

    def test_warning_disabled_when_budget_disabled(self):
        bm = BudgetManager(BudgetConfig(enabled=False, total_tokens=1000))
        assert bm.check_warning() is False

    def test_record_usage_thread_safety(self):
        bm = BudgetManager(BudgetConfig(total_tokens=1_000_000))
        errors = []

        def worker():
            try:
                for _ in range(1000):
                    bm.record_usage(1, 1)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert bm.used_input_tokens == 10000
        assert bm.used_output_tokens == 10000

    def test_to_dict_serialization(self):
        bm = BudgetManager(BudgetConfig(total_tokens=1000))
        bm.record_usage(300, 200)
        d = bm.to_dict()
        assert d["total_budget"] == 1000
        assert d["used_input_tokens"] == 300
        assert d["used_output_tokens"] == 200
        assert d["used_total_tokens"] == 500
        assert d["remaining_tokens"] == 500
        assert d["usage_fraction"] == 0.5
        assert d["enabled"] is True

    def test_disabled_budget_always_passes(self):
        bm = BudgetManager(BudgetConfig(enabled=False, total_tokens=100))
        assert bm.check() is True
        bm.record_usage(9999, 9999)
        assert bm.check() is True
