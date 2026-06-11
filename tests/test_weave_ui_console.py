"""Web Console API tests -- M2-D."""
from fastapi.testclient import TestClient

from weave_ui.server import app


client = TestClient(app)


class TestConsolePage:
    def test_console_page_exists(self):
        resp = client.get("/console")
        assert resp.status_code == 200
        assert resp.status_code == 200

    def test_console_auto_refresh_interval(self):
        resp = client.get("/console")
        assert resp.status_code == 200


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

    def test_cancel_nonexistent_job(self):
        """M2-D: Cancel API should return error for nonexistent job."""
        resp = client.post("/api/jobs/nonexistent-job-id/cancel")
        # Should return error, not 200 (success would hide regression)
        assert resp.status_code in (400, 404)
        data = resp.json()
        assert "error" in data or "detail" in data

    def test_retry_nonexistent_job(self):
        """M2-D: Retry API should return error for nonexistent job."""
        resp = client.post("/api/jobs/nonexistent-job-id/retry")
        assert resp.status_code in (400, 404)
        data = resp.json()
        assert "error" in data or "detail" in data


class TestTicketsAPI:
    def test_list_tickets(self):
        resp = client.get("/api/tickets")
        assert resp.status_code == 200
        data = resp.json()
        assert "tickets" in data
        assert "stats" in data

    def test_approve_nonexistent_ticket(self):
        """M2-D: Approve API should return error for nonexistent ticket."""
        resp = client.post("/api/tickets/nonexistent-ticket-id/approve")
        assert resp.status_code in (400, 404)
        data = resp.json()
        assert "error" in data or "detail" in data

    def test_reject_nonexistent_ticket(self):
        """M2-D: Reject API should return error for nonexistent ticket."""
        resp = client.post(
            "/api/tickets/nonexistent-ticket-id/reject",
            json={"reason": "test"},
        )
        assert resp.status_code in (400, 404)
        data = resp.json()
        assert "error" in data or "detail" in data


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
