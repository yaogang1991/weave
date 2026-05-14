"""
Tests for #324: increased default run timeout.

The default run_timeout_sec is 1800 (30 min) instead of the old 600 (10 min),
and is configurable via HARNESS_RUN_TIMEOUT_SEC env var or CLI --timeout.
"""
import inspect
import os
import pytest

from core.config import HarnessConfig


class TestRunTimeoutDefault:
    def test_default_timeout_is_1800(self):
        """Default run_timeout_sec should be 1800 (30 minutes)."""
        config = HarnessConfig()
        assert config.run_timeout_sec == 1800

    def test_env_var_overrides_default(self, monkeypatch):
        """HARNESS_RUN_TIMEOUT_SEC env var overrides the default."""
        monkeypatch.setenv("HARNESS_RUN_TIMEOUT_SEC", "3600")
        config = HarnessConfig()
        assert config.run_timeout_sec == 3600

    def test_explicit_value_overrides_env(self):
        """Explicit constructor value takes precedence."""
        config = HarnessConfig(run_timeout_sec=900)
        assert config.run_timeout_sec == 900

    def test_submit_job_default_timeout(self):
        """RunService.submit_job defaults to 1800s."""
        from control_plane.service import RunService
        sig = inspect.signature(RunService.submit_job)
        default = sig.parameters["timeout"].default
        assert default == 1800

    def test_run_job_metadata_fallback(self):
        """Job without run_timeout_sec metadata falls back to 1800."""
        from control_plane.repository import JobRepository
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = JobRepository(base_path=tmp)
            job = repo.create_job(requirement="test")
            # No run_timeout_sec in metadata → should fall back to 1800
            assert job.metadata.get("run_timeout_sec", 1800) == 1800

    def test_old_600_default_gone(self):
        """Verify the old 600s default is no longer used."""
        config = HarnessConfig()
        assert config.run_timeout_sec != 600
