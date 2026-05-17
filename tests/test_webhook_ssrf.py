"""Tests for webhook URL SSRF validation (#495)."""
import pytest

from monitoring.alerts import AlertManager


class TestWebhookSSRFProtection:
    """Verify webhook URLs are validated against SSRF attacks (#495)."""

    @pytest.mark.parametrize("url", [
        "http://localhost:8080/steal",
        "http://127.0.0.1:8080/api/tickets/t1/approve",
        "http://0.0.0.0/anything",
        "http://[::1]/test",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/internal",
        "http://172.16.0.1/internal",
        "http://192.168.1.1/internal",
    ])
    def test_blocks_private_and_internal_urls(self, url):
        reason = AlertManager._validate_webhook_url(url)
        assert reason is not None, f"URL should be blocked: {url}"

    @pytest.mark.parametrize("url", [
        "https://hooks.slack.com/services/xxx",
        "https://discord.com/api/webhooks/xxx",
        "https://example.com/alerts",
        "http://example.com/webhook",
    ])
    def test_allows_public_urls(self, url):
        reason = AlertManager._validate_webhook_url(url)
        assert reason is None, f"URL should be allowed: {url}"

    def test_blocks_javascript_scheme(self):
        assert AlertManager._validate_webhook_url("javascript:alert(1)") is not None

    def test_blocks_file_scheme(self):
        assert AlertManager._validate_webhook_url("file:///etc/passwd") is not None

    def test_blocks_empty_url(self):
        assert AlertManager._validate_webhook_url("") is not None

    def test_blocks_missing_hostname(self):
        assert AlertManager._validate_webhook_url("http://") is not None
