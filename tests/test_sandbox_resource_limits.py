"""Tests for LocalSandbox resource limits (#482)."""
import sys

import pytest

from backend.sandbox import LocalSandbox


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="resource.setrlimit not available on Windows",
)
class TestResourceLimits:
    """Verify LocalSandbox applies resource limits to child processes."""

    @pytest.mark.asyncio
    async def test_normal_command_succeeds(self):
        """Normal commands complete successfully with limits."""
        sandbox = LocalSandbox(memory_limit_mb=512, cpu_limit_sec=30)
        result = await sandbox.run_command("echo hello", cwd="/tmp")
        assert result.success
        assert "hello" in result.stdout

    @pytest.mark.asyncio
    async def test_configurable_memory_limit(self):
        """Memory limit is configurable via constructor."""
        sandbox = LocalSandbox(memory_limit_mb=64)
        assert sandbox._memory_limit_mb == 64

    @pytest.mark.asyncio
    async def test_configurable_cpu_limit(self):
        """CPU limit is configurable via constructor."""
        sandbox = LocalSandbox(cpu_limit_sec=60)
        assert sandbox._cpu_limit_sec == 60

    @pytest.mark.asyncio
    async def test_preexec_fn_returns_callable(self):
        """_make_preexec_fn returns a callable on supported platforms."""
        sandbox = LocalSandbox()
        fn = sandbox._make_preexec_fn()
        assert fn is not None
        assert callable(fn)

    @pytest.mark.asyncio
    async def test_explicit_env_not_stripped(self):
        """Explicit env dict is passed through, resource limits still applied."""
        sandbox = LocalSandbox()
        result = await sandbox.run_command(
            "echo $MY_VAR", cwd="/tmp", env={"MY_VAR": "test_value"},
        )
        assert "test_value" in result.stdout

    @pytest.mark.asyncio
    async def test_timeout_still_works(self):
        """Timeout works with resource limits."""
        sandbox = LocalSandbox(cpu_limit_sec=300)
        result = await sandbox.run_command("sleep 10", cwd="/tmp", timeout=1)
        assert not result.success
        assert "timed out" in result.stderr.lower()


class TestResourceLimitsDefaults:
    """Verify default limit values."""

    def test_default_memory_limit(self):
        assert LocalSandbox.DEFAULT_MEMORY_LIMIT_MB == 2048

    def test_default_cpu_limit(self):
        assert LocalSandbox.DEFAULT_CPU_LIMIT_SEC == 300
