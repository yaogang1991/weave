"""Tests for visualizer API key authentication (#494)."""
import os

import pytest
from fastapi.testclient import TestClient

from visualizer.server import app


@pytest.fixture(autouse=True)
def clean_env():
    """Ensure WEAVE_API_KEY is unset before and after each test."""
    original = os.environ.pop("WEAVE_API_KEY", None)
    yield
    if original is not None:
        os.environ["WEAVE_API_KEY"] = original
    else:
        os.environ.pop("WEAVE_API_KEY", None)


@pytest.fixture
def client():
    return TestClient(app)


class TestApiKeyAuth:
    def test_no_key_configured_allows_access(self, client):
        """Without WEAVE_API_KEY, all endpoints are accessible."""
        resp = client.get("/api/health")
        assert resp.status_code == 200

    def test_public_paths_no_auth_needed(self, client):
        """Public paths (/api/health, /ws) don't require auth."""
        os.environ["WEAVE_API_KEY"] = "test-secret"
        resp = client.get("/api/health")
        assert resp.status_code == 200

    def test_protected_endpoint_requires_key(self, client):
        """Protected endpoints require valid API key."""
        os.environ["WEAVE_API_KEY"] = "test-secret"
        resp = client.get("/api/sessions")
        assert resp.status_code == 401

    def test_valid_key_grants_access(self, client):
        """Valid API key in header grants access."""
        os.environ["WEAVE_API_KEY"] = "test-secret"
        resp = client.get(
            "/api/sessions",
            headers={"X-API-Key": "test-secret"},
        )
        assert resp.status_code == 200

    def test_invalid_key_denied(self, client):
        """Wrong API key is rejected."""
        os.environ["WEAVE_API_KEY"] = "test-secret"
        resp = client.get(
            "/api/sessions",
            headers={"X-API-Key": "wrong-key"},
        )
        assert resp.status_code == 401

    def test_query_param_key_accepted(self, client):
        """API key can also be passed as query parameter."""
        os.environ["WEAVE_API_KEY"] = "test-secret"
        resp = client.get("/api/sessions?api_key=test-secret")
        assert resp.status_code == 200

    def test_mutations_require_auth(self, client):
        """Write endpoints also require authentication."""
        os.environ["WEAVE_API_KEY"] = "test-secret"
        resp = client.post("/api/jobs/nonexistent/cancel")
        assert resp.status_code == 401

        resp = client.post(
            "/api/jobs/nonexistent/cancel",
            headers={"X-API-Key": "test-secret"},
        )
        # Auth passes, but job doesn't exist — 404 or 400 (bad request)
        assert resp.status_code in (404, 200, 400)
