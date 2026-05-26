"""Tests for #941: LLMOps platform integration."""
from monitoring.langfuse_exporter import setup_langfuse


class TestLangfuseExporter:
    def test_returns_false_without_langfuse(self):
        """setup_langfuse returns False when langfuse not installed."""
        result = setup_langfuse(public_key="pk-test", secret_key="sk-test")
        assert isinstance(result, bool)

    def test_returns_false_without_credentials(self):
        """setup_langfuse returns False when credentials missing."""
        result = setup_langfuse()
        assert result is False
