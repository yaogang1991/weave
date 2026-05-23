"""Tests for credential isolation in LocalSandbox (#456)."""
import os

import pytest

from backend.sandbox import LocalSandbox


class TestCredentialIsolation:
    """Verify sandbox processes cannot access API keys."""

    def setup_method(self):
        self.sandbox = LocalSandbox()

    @pytest.mark.asyncio
    async def test_default_env_strips_sensitive_keys(self):
        """When env=None, sensitive keys are filtered out."""
        original = os.environ.copy()
        os.environ["ANTHROPIC_API_KEY"] = "sk-test-secret-123"
        os.environ["OPENAI_API_KEY"] = "sk-openai-secret"
        os.environ["GITHUB_TOKEN"] = "ghp_test_token"
        os.environ["MY_CUSTOM_VAR"] = "safe_value"
        try:
            result = await self.sandbox.run_command(
                "python -c \"import os; print(os.environ.get('ANTHROPIC_API_KEY',''), os.environ.get('OPENAI_API_KEY',''), os.environ.get('GITHUB_TOKEN',''), os.environ.get('MY_CUSTOM_VAR',''))\"",
                cwd=".",
                timeout=5,
            )
            output = result.stdout.strip()
            assert "sk-test-secret-123" not in output
            assert "sk-openai-secret" not in output
            assert "ghp_test_token" not in output
            assert "safe_value" in output
        finally:
            os.environ.clear()
            os.environ.update(original)

    @pytest.mark.asyncio
    async def test_explicit_env_is_not_filtered(self):
        """When env is explicitly passed, use it as-is (trust caller)."""
        result = await self.sandbox.run_command(
            "python -c \"import os; print(os.environ.get('MY_VAR',''))\"",
            cwd=".",
            timeout=5,
            env={"MY_VAR": "explicit_value", "PATH": os.environ.get("PATH", "")},
        )
        assert "explicit_value" in result.stdout

    def test_build_safe_env_excludes_known_prefixes(self):
        """_build_safe_env filters all configured sensitive prefixes."""
        original = os.environ.copy()
        os.environ["ANTHROPIC_API_KEY"] = "secret1"
        os.environ["OPENAI_API_KEY"] = "secret2"
        os.environ["AWS_ACCESS_KEY_ID"] = "secret3"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "secret4"
        os.environ["API_KEY"] = "secret5"
        os.environ["TOKEN"] = "secret6"
        os.environ["PASSWORD"] = "secret7"
        os.environ["SECRET_KEY"] = "secret8"
        os.environ["SAFE_VAR"] = "public"
        try:
            safe_env = self.sandbox._build_safe_env()
            assert "ANTHROPIC_API_KEY" not in safe_env
            assert "OPENAI_API_KEY" not in safe_env
            assert "AWS_ACCESS_KEY_ID" not in safe_env
            assert "AWS_SECRET_ACCESS_KEY" not in safe_env
            assert "API_KEY" not in safe_env
            assert "TOKEN" not in safe_env
            assert "PASSWORD" not in safe_env
            assert "SECRET_KEY" not in safe_env
            assert safe_env.get("SAFE_VAR") == "public"
        finally:
            os.environ.clear()
            os.environ.update(original)

    def test_build_safe_env_preserves_path_and_home(self):
        """_build_safe_env keeps essential vars like PATH, HOME."""
        safe_env = self.sandbox._build_safe_env()
        assert "PATH" in safe_env
        assert "HOME" in safe_env
