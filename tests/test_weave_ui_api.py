"""Integration tests for Weave UI API endpoints (M8.5)."""

import pytest
from fastapi.testclient import TestClient
from weave_ui.server import app

client = TestClient(app)


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_list_jobs():
    r = client.get("/api/jobs")
    assert r.status_code == 200
    assert "jobs" in r.json()


def test_list_sessions():
    r = client.get("/api/sessions")
    assert r.status_code == 200


def test_list_tickets():
    r = client.get("/api/tickets")
    assert r.status_code == 200


def test_notification_prefs():
    r = client.get("/api/notification-preferences")
    assert r.status_code == 200
    prefs = r.json()
    assert "on_succeeded" in prefs
    r2 = client.put("/api/notification-preferences", json={**prefs, "on_failed": False})
    assert r2.status_code == 200
    assert r2.json()["on_failed"] is False


def test_search():
    r = client.get("/api/search", params={"q": "test"})
    assert r.status_code == 200
    data = r.json()
    assert "jobs" in data
    assert "count" in data


def test_workspaces():
    r = client.get("/api/workspaces")
    assert r.status_code == 200
    assert "workspaces" in r.json()


def test_task_templates():
    r = client.get("/api/task-templates")
    assert r.status_code == 200
    assert "templates" in r.json()


def test_job_not_found():
    r = client.get("/api/jobs/nonexistent_id")
    assert r.status_code == 404


def test_annotations_default():
    r = client.get("/api/jobs/test-job-123/annotations")
    assert r.status_code == 200
    data = r.json()
    assert "job_id" in data
