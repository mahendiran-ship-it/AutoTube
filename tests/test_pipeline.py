"""End-to-end smoke pipeline + publishing/analytics logic with mocked YouTube services."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.analytics.collector import AnalyticsCollector
from app.analytics.optimizer import PerformanceOptimizer
from app.analytics.reports import ReportGenerator
from app.pipeline.orchestrator import Pipeline
from app.pipeline.services import build_services
from app.publishing.scheduler import next_publish_slot, to_rfc3339_utc
from app.publishing.uploader import PublishRefused, Publisher
from tests.conftest import needs_ffmpeg


@pytest.fixture
def svc(cfg):
    return build_services(cfg, setup_log=False)


@needs_ffmpeg
def test_smoke_pipeline_runs_and_is_never_publishable(svc):
    pl = Pipeline(svc)
    rid = pl.start(smoke=True)
    res = pl.execute(rid)
    stages = {s["stage"]: s["status"] for s in res["stages"]}
    assert stages.get("render") == "completed", res
    vid = res["video_id"]
    v = svc.repo.get_video(vid)
    assert v["is_smoke"] == 1
    # publisher must refuse smoke videos
    pub = Publisher(svc.cfg, svc.repo, auth=None, notifier=svc.notifier, service_factory=lambda: None)
    with pytest.raises(PublishRefused):
        pub.publish(vid, "private")


def test_next_slot_respects_capacity_and_timezone(cfg):
    now = datetime(2026, 11, 21, 12, 30, tzinfo=timezone.utc)   # 18:00 IST
    s1 = next_publish_slot(cfg, [], now=now)
    assert (s1.hour, s1.minute) == (18, 30) and s1.date().isoformat() == "2026-11-21"
    s2 = next_publish_slot(cfg, [to_rfc3339_utc(s1)], now=now)
    assert s2.date().isoformat() == "2026-11-22"
    late = datetime(2026, 11, 21, 13, 0, tzinfo=timezone.utc)   # 18:30 IST: within lead time -> tomorrow
    assert next_publish_slot(cfg, [], now=late).date().isoformat() == "2026-11-22"


class FakeReq:
    def __init__(self, fn): self.fn = fn
    def execute(self): return self.fn()
    def next_chunk(self): return None, self.fn()


class FakeYT:
    def __init__(self, privacy="private", publish_at=None):
        self.privacy, self.publish_at, self.inserted = privacy, publish_at, []
    def videos(self): return self
    def channels(self): return self
    def playlistItems(self): return self
    def thumbnails(self): return self
    def insert(self, part, body, media_body):
        self.inserted.append(body)
        self.publish_at = body["status"].get("publishAt")
        return FakeReq(lambda: {"id": "abc123"})
    def list(self, **kw):
        if "mine" in kw:
            return FakeReq(lambda: {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU"}}}]})
        if "playlistId" in kw:
            return FakeReq(lambda: {"items": []})
        return FakeReq(lambda: {"items": [{"status": {"privacyStatus": self.privacy, "publishAt": self.publish_at}}]})
    def set(self, **kw): return FakeReq(lambda: {})


def _ready_video(svc, tmp_path):
    f = tmp_path / "v.mp4"; f.write_bytes(b"x" * 100)
    vid = svc.repo.create_video(None, "Why Black Holes Spin", "REVIEW")
    svc.repo.update_video(vid, final_path=str(f), content_hash="h1", status="qc_passed",
                          qc_json={"passed": True, "critical_failures": []},
                          metadata_json={"title": "Why Black Holes Spin", "description": "A long enough description of this short video. #space",
                                         "tags": ["space"], "category_id": "28"})
    return vid


def test_publish_is_idempotent_and_verifies(svc, tmp_path):
    vid = _ready_video(svc, tmp_path)
    yt = FakeYT()
    pub = Publisher(svc.cfg, svc.repo, None, svc.notifier, service_factory=lambda: yt, sleep=lambda s: None)
    r = pub.publish(vid, "public", schedule=True)
    assert r["status"] == "scheduled" and yt.inserted[0]["status"]["privacyStatus"] == "private"
    assert "publishAt" in yt.inserted[0]["status"]
    assert yt.inserted[0]["status"]["containsSyntheticMedia"] is False
    r2 = pub.publish(vid, "public", schedule=True)
    assert r2["already_uploaded"] and len(yt.inserted) == 1


def test_publish_reports_when_youtube_forces_private(svc, tmp_path):
    vid = _ready_video(svc, tmp_path)
    yt = FakeYT()
    yt.list = lambda **kw: FakeReq(lambda: {"items": [{"status": {"privacyStatus": "private"}}]}) if "id" in kw else FakeYT.list(yt, **kw)
    pub = Publisher(svc.cfg, svc.repo, None, svc.notifier, service_factory=lambda: yt, sleep=lambda s: None)
    r = pub.publish(vid, "public", schedule=True)
    assert r["status"] == "uploaded" and r["warnings"]


def test_publish_refuses_failed_qc(svc, tmp_path):
    vid = _ready_video(svc, tmp_path)
    svc.repo.update_video(vid, qc_json={"passed": False})
    pub = Publisher(svc.cfg, svc.repo, None, svc.notifier, service_factory=lambda: FakeYT())
    with pytest.raises(PublishRefused):
        pub.publish(vid, "private")


class FakeAnalytics:
    def __init__(self, rows): self.rows = rows
    def reports(self): return self
    def query(self, **kw):
        m = kw["metrics"].split(",")
        if "estimatedRevenue" in m:
            raise RuntimeError("forbidden")
        return FakeReq(lambda: {"columnHeaders": [{"name": x} for x in m], "rows": [self.rows[x] for x in [m]] if False else [[self.rows.get(x, 0) for x in m]]})


def test_collector_stores_null_for_unavailable_metrics(svc, tmp_path):
    vid = _ready_video(svc, tmp_path)
    up = svc.repo.create_upload(vid, "public", None)
    svc.repo.update_upload(up, youtube_video_id="abc123", status="published")
    an = FakeAnalytics({"views": 120, "averageViewPercentage": 71.5, "likes": 9})
    col = AnalyticsCollector(svc.cfg, svc.repo, None, svc.notifier, service_factory=lambda: an)
    col.collect_video(svc.repo.uploads_for_video(vid)[0], an)
    a = svc.repo.latest_analytics()[0]
    assert a["views"] == 120 and a["impressions"] is None and a["ctr"] is None
    assert a["revenue"] is None and a["revenue_status"] == "unavailable"


def test_optimizer_needs_enough_samples_and_bounds_weights(svc):
    opt = PerformanceOptimizer(svc.cfg, svc.repo, None)
    assert opt.run()["applied"] is False
    n = svc.cfg.get("optimizer.min_samples_for_conclusion")
    for cat, views, pct in (("missions", 500, 80.0), ("mysteries", 50, 40.0)):
        for i in range(n):
            t = svc.repo.insert("topics", title=f"{cat} {i}", fingerprint=f"{cat}{i}", category=cat)
            v = svc.repo.create_video(t, f"{cat} {i}", "REVIEW")
            svc.repo.upsert_analytics(v, f"y{cat}{i}", "2026-11-21", views=views, avg_view_percentage=pct)
    r = opt.run()
    w = r["weights"]
    assert r["applied"] and w["missions"] > 1.0 > w["mysteries"]
    assert all(0.8 <= x <= 1.25 for x in w.values())
    assert all(abs(x - 1.0) <= svc.cfg.get("optimizer.max_weight_step") + 1e-9 for x in w.values())
    assert svc.repo.list_strategy_changes()


def test_report_marks_missing_data(svc):
    rep = ReportGenerator(svc.cfg, svc.repo).generate("daily", save=False)
    assert "No channel statistics" in rep["markdown"] and "not available" in rep["markdown"]
