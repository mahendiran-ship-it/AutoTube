"""Dashboard API tests (TestClient, no network)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.scheduler import Scheduler
from app.dashboard.api import create_app
from app.pipeline.orchestrator import Pipeline
from app.pipeline.services import build_services

H = {"X-Requested-With": "autotube"}


@pytest.fixture
def client(cfg):
    svc = build_services(cfg, setup_log=False)
    sched = Scheduler(svc, lambda: Pipeline(svc))
    app = create_app(svc, sched, lambda: Pipeline(svc))
    c = TestClient(app, base_url="http://localhost")
    c.svc, c.sched = svc, sched
    return c


def test_index_and_status(client):
    assert "AutoTube" in client.get("/").text
    r = client.get("/api/status")
    assert r.status_code == 200 and "problems" in r.json()
    assert r.json()["publishing_mode"] in ("REVIEW", "PRIVATE", "MANUAL", "AUTO")


def test_host_header_and_csrf_guard(client):
    assert client.get("/api/status", headers={"Host": "evil.example"}).status_code == 403
    assert client.post("/api/automation/start").status_code == 403           # missing header
    assert client.post("/api/automation/start", headers={**H, "Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/automation/start", headers=H).json()["automation_enabled"] is True


def test_config_whitelist_and_validation(client):
    assert client.post("/api/config", headers=H, json={"paths": {"data_dir": "/etc"}}).status_code == 400
    assert client.post("/api/config", headers=H, json={"pipeline": {"publishing_mode": "YOLO"}}).status_code == 400
    assert client.post("/api/config", headers=H, json={"pipeline": {"publishing_mode": "PRIVATE"}}).status_code == 200
    assert client.get("/api/config").json()["pipeline"]["publishing_mode"] == "PRIVATE"
    assert client.post("/api/config", headers=H, json={"channel": {"api_key": "x"}}).status_code in (400, 500)


def test_topics_add_and_reject(client):
    r = client.post("/api/topics/add", headers=H, json={"title": "Why Europa may hide an ocean"})
    tid = r.json()["id"]
    assert client.post("/api/topics/add", headers=H, json={"title": "Why Europa may hide an ocean"}).status_code == 409
    assert client.post(f"/api/topics/{tid}/reject", headers=H, json={}).status_code == 200
    assert any(t["id"] == tid and t["status"] == "rejected" for t in client.get("/api/topics").json())


def test_media_path_is_confined(client, tmp_path):
    vid = client.svc.repo.create_video(None, "x", "REVIEW")
    secret = tmp_path / "secret.mp4"
    secret.write_bytes(b"x")
    client.svc.repo.update_video(vid, final_path=str(secret))      # outside output/
    assert client.get(f"/media/video/{vid}").status_code == 404


def test_analytics_and_reports_endpoints_without_data(client):
    assert client.get("/api/analytics").json()["videos"] == []
    assert "markdown" in client.get("/api/reports/weekly").json()
    assert client.post("/api/optimizer/run", headers=H, json={}).json()["applied"] is False


def test_token_required_when_set(cfg, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "s3cret")
    svc = build_services(cfg, setup_log=False)
    app = create_app(svc, Scheduler(svc, lambda: Pipeline(svc)), lambda: Pipeline(svc))
    c = TestClient(app, base_url="http://localhost")
    assert c.get("/api/status").status_code == 401
    assert c.get("/api/status", headers={"X-Dashboard-Token": "s3cret"}).status_code == 200
    assert c.post("/api/login", headers=H, json={"token": "bad"}).status_code == 401


def test_scheduler_tick_fires_once_per_day(client):
    s = client.sched
    assert s.tick() == []                                    # automation off
    s.set_automation(True)
    s.cfg.set("schedule.production_time", "00:00")
    s.cfg.set("schedule.analytics_time", "00:00")
    fired = s.tick()
    assert "production" in fired
    assert "production" not in s.tick()                       # not repeated the same day
