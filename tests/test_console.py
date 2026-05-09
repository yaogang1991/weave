"""Web Console API tests."""
import pytest
from fastapi.testclient import TestClient

from visualizer.server import app


client = TestClient(app)


class TestConsolePage:
    def test_console_page_exists(self):
        resp = client.get("/console")
        assert resp.status_code == 200
        assert "Harness Console" in resp.text


class TestJobsAPI:
    def test_list_jobs(self):
        resp = client.get("/api/jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert "jobs" in data
        assert "count" in data

    def test_list_jobs_with_status_filter(self):
        resp = client.get("/api/jobs?status=queued")
        assert resp.status_code == 200
        data = resp.json()
        for job in data["jobs"]:
            assert job["status"] == "queued"


class TestTicketsAPI:
    def test_list_tickets(self):
        resp = client.get("/api/tickets")
        assert resp.status_code == 200
        data = resp.json()
        assert "tickets" in data
        assert "stats" in data


class TestMetricsAPI:
    def test_metrics(self):
        resp = client.get("/api/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "summary" in data

    def test_alerts(self):
        resp = client.get("/api/alerts")
        assert resp.status_code == 200
        data = resp.json()
        assert "alerts" in data


class TestOperationsAPI:
    def test_recover(self):
        resp = client.post("/api/recover")
        assert resp.status_code == 200
        data = resp.json()
        assert "recovered_count" in data
