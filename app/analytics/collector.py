"""Collects REAL analytics from the YouTube Data/Analytics APIs. Nothing is estimated or invented.

Honesty rules:
- The Analytics API exposes no thumbnail-impressions or impressions-CTR metric -> stored as NULL.
- Revenue is stored only if the API returns it (needs a monetized channel + the monetary scope);
  otherwise revenue_status='unavailable' and revenue stays NULL (never 0).
- Retention curve comes from audienceWatchRatio / relativeRetentionPerformance by elapsedVideoTimeRatio.
Data is delayed by YouTube (typically 1-3 days); recent days may be incomplete.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core import notifications as N
from app.core.errors import QuotaExceeded
from app.core.logger import get_logger

log = get_logger("analytics")

CORE_METRICS = ["views", "estimatedMinutesWatched", "averageViewDuration", "averageViewPercentage",
                "likes", "comments", "subscribersGained", "subscribersLost"]


def _row_dict(resp: dict) -> dict:
    heads = [h["name"] for h in resp.get("columnHeaders", [])]
    rows = resp.get("rows") or []
    return dict(zip(heads, rows[0])) if rows else {}


class AnalyticsCollector:
    def __init__(self, cfg, repo, auth, notifier, service_factory=None):
        self.cfg, self.repo, self.auth, self.notifier = cfg, repo, auth, notifier
        self._yt_factory = service_factory or (lambda: auth.service())
        self._an_factory = service_factory or (lambda: auth.analytics_service())

    def _today(self) -> str:
        return datetime.now(timezone.utc).astimezone(self.cfg.tz).date().isoformat()

    def _query(self, an, **kw) -> dict:
        try:
            r = an.reports().query(ids="channel==MINE", **kw).execute()
            self.repo.add_api_usage("youtube_analytics", "reports.query", True)
            return r
        except Exception as e:
            self.repo.add_api_usage("youtube_analytics", "reports.query", False, error=type(e).__name__)
            raise

    # ---- per video ---------------------------------------------------------------
    def collect_video(self, upload: dict, an=None) -> dict:
        yt_id = upload["youtube_video_id"]
        an = an or self._an_factory()
        v = self.repo.get_video(upload["video_id"]) or {}
        start = (v.get("created_at") or "2020-01-01")[:10]
        end = self._today()
        r = self._query(an, startDate=start, endDate=end, metrics=",".join(CORE_METRICS), filters=f"video=={yt_id}")
        d = _row_dict(r)
        metrics = {
            "views": d.get("views"), "watch_minutes": d.get("estimatedMinutesWatched"),
            "avg_view_duration": d.get("averageViewDuration"), "avg_view_percentage": d.get("averageViewPercentage"),
            "likes": d.get("likes"), "comments": d.get("comments"),
            "subs_gained": d.get("subscribersGained"), "subs_lost": d.get("subscribersLost"),
            "impressions": None, "ctr": None,      # not exposed by the Analytics API
            "revenue": None, "revenue_status": "unavailable",
            "raw_json": {"core": d, "range": [start, end]},
        }
        if self.cfg.get("youtube.request_monetary_scope", False):
            try:
                rr = _row_dict(self._query(an, startDate=start, endDate=end, metrics="estimatedRevenue", filters=f"video=={yt_id}"))
                if rr.get("estimatedRevenue") is not None:
                    metrics["revenue"], metrics["revenue_status"] = float(rr["estimatedRevenue"]), "reported"
            except Exception as e:
                log.info("revenue not available for %s (%s)", yt_id, type(e).__name__)
        if self.cfg.get("analytics.retention_snapshot", True) and (d.get("views") or 0) >= 10:
            try:
                rr = self._query(an, startDate=start, endDate=end, dimensions="elapsedVideoTimeRatio",
                                 metrics="audienceWatchRatio,relativeRetentionPerformance", filters=f"video=={yt_id}")
                heads = [h["name"] for h in rr.get("columnHeaders", [])]
                metrics["retention_json"] = [dict(zip(heads, row)) for row in rr.get("rows", [])]
            except Exception as e:
                log.info("retention curve unavailable for %s (%s)", yt_id, type(e).__name__)
        self.repo.upsert_analytics(upload["video_id"], yt_id, self._today(), **metrics)
        return metrics

    def collect_all(self) -> dict:
        ups = [u for u in self.repo.list_uploads(500)
               if u.get("youtube_video_id") and u["status"] in ("scheduled", "published", "uploaded")]
        # scheduled videos that are still private have no analytics yet
        ups = [u for u in ups if u["status"] == "published" or u.get("privacy") == "public"]
        res = {"collected": 0, "failed": 0, "errors": []}
        if not ups:
            res["note"] = "no published videos yet"
            return res
        an = self._an_factory()
        for u in ups:
            try:
                self.collect_video(u, an)
                res["collected"] += 1
            except Exception as e:
                res["failed"] += 1
                res["errors"].append(f"{u['youtube_video_id']}: {type(e).__name__}")
        try:
            res["channel"] = self.collect_channel(an)
        except Exception as e:
            res["errors"].append(f"channel: {type(e).__name__}")
        return res

    # ---- channel -----------------------------------------------------------------
    def collect_channel(self, an=None) -> dict:
        yt = self._yt_factory()
        r = yt.channels().list(part="statistics", mine=True).execute()
        self.repo.add_api_usage("youtube", "channels.list", True)
        items = r.get("items") or []
        if not items:
            raise RuntimeError("no channel found for the authorized account")
        st = items[0].get("statistics", {})
        an = an or self._an_factory()
        today = self._today()
        d28 = (datetime.fromisoformat(today) - timedelta(days=28)).date().isoformat()
        d365 = (datetime.fromisoformat(today) - timedelta(days=365)).date().isoformat()
        m28 = _row_dict(self._query(an, startDate=d28, endDate=today, metrics="estimatedMinutesWatched")).get("estimatedMinutesWatched")
        m365 = _row_dict(self._query(an, startDate=d365, endDate=today, metrics="estimatedMinutesWatched")).get("estimatedMinutesWatched")
        out = {"subscribers": None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0)),
               "total_views": int(st.get("viewCount", 0)), "video_count": int(st.get("videoCount", 0)),
               "watch_minutes_28d": m28, "watch_hours_365d": (m365 / 60.0) if m365 is not None else None,
               "raw_json": {"statistics": st}}
        self.repo.save_channel_stats(today, **out)
        return out

    # ---- monetization progress (facts only; thresholds come from config and may be outdated) --------
    def monetization_progress(self) -> dict:
        hist = self.repo.channel_stats_history(1)
        cur = hist[0] if hist else None
        tiers = []
        for t in self.cfg.get("monetization.tiers") or []:
            row = {"name": t["name"], "requirements": {}, "met": None}
            if cur:
                checks = []
                if "subscribers" in t:
                    val = cur.get("subscribers")
                    row["requirements"]["subscribers"] = {"need": t["subscribers"], "have": val}
                    checks.append(None if val is None else val >= t["subscribers"])
                if "watch_hours_365d" in t:
                    val = cur.get("watch_hours_365d")
                    row["requirements"]["watch_hours_365d"] = {"need": t["watch_hours_365d"], "have": None if val is None else round(val, 1)}
                    checks.append(None if val is None else val >= t["watch_hours_365d"])
                if "shorts_views_90d" in t:
                    row["requirements"]["shorts_views_90d"] = {"need": t["shorts_views_90d"], "have": None,
                                                              "note": "Shorts views are not exposed separately by the API; check YouTube Studio"}
                    checks.append(None)
                if "uploads_90d" in t:
                    row["requirements"]["uploads_90d"] = {"need": t["uploads_90d"], "have": self._uploads_90d()}
                    checks.append(row["requirements"]["uploads_90d"]["have"] >= t["uploads_90d"])
                row["met"] = None if (not checks or any(c is None for c in checks)) else all(checks)
            tiers.append(row)
        return {"as_of": cur["snapshot_date"] if cur else None, "tiers": tiers,
                "disclaimer": "Thresholds are configurable and change over time. Eligibility is decided only by YouTube - "
                              "verify at https://support.google.com/youtube/answer/72851 and in YouTube Studio."}

    def _uploads_90d(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        n = 0
        for u in self.repo.list_uploads(500):
            if u["status"] in ("scheduled", "published", "uploaded") and u.get("created_at"):
                try:
                    if datetime.fromisoformat(u["created_at"].replace("Z", "+00:00")) >= cutoff:
                        n += 1
                except ValueError:
                    pass
        return n
