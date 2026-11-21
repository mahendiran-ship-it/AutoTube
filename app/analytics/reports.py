"""Daily / weekly reports built only from stored real data. Missing metrics are shown as 'n/a'."""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.logger import get_logger

log = get_logger("reports")


def _fmt(v, nd=0, suffix=""):
    if v is None:
        return "n/a"
    return f"{v:,.{nd}f}{suffix}"


class ReportGenerator:
    def __init__(self, cfg, repo):
        self.cfg, self.repo = cfg, repo

    def build(self, kind: str = "daily") -> dict:
        latest = [a for a in self.repo.latest_analytics()]
        videos = {v["id"]: v for v in self.repo.list_videos(limit=500)}
        rows = []
        for a in latest:
            v = videos.get(a.get("video_id"), {})
            if v.get("is_smoke"):
                continue
            rows.append({"title": v.get("title") or a["youtube_video_id"], "youtube_video_id": a["youtube_video_id"],
                         "views": a.get("views"), "avg_view_percentage": a.get("avg_view_percentage"),
                         "likes": a.get("likes"), "subs_gained": a.get("subs_gained"), "as_of": a["snapshot_date"],
                         "revenue": a.get("revenue"), "revenue_status": a.get("revenue_status")})
        rows.sort(key=lambda r: -(r["views"] or 0))
        hist = self.repo.channel_stats_history(8 if kind == "weekly" else 2)
        cur, prev = (hist[0] if hist else None), (hist[-1] if len(hist) > 1 else None)
        uploads = self.repo.list_uploads(200)
        counts = {s: sum(1 for u in uploads if u["status"] == s) for s in ("uploaded", "scheduled", "published", "failed")}
        return {"kind": kind, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "channel": cur, "channel_previous": prev, "videos": rows, "upload_counts": counts,
                "cost_inr_month": round(self.repo.month_cost_inr(), 2),
                "notes": ["Impressions and CTR are not available through the YouTube Analytics API and are not reported.",
                          "Revenue is shown only when YouTube reports it for a monetized channel.",
                          "YouTube analytics lag by 1-3 days."]}

    def markdown(self, rep: dict) -> str:
        L = [f"# {self.cfg.get('channel.name', 'Channel')} — {rep['kind']} report", f"_Generated {rep['generated_at']}_", ""]
        c, p = rep["channel"], rep["channel_previous"]
        if c:
            d = (c["subscribers"] - p["subscribers"]) if (p and c.get("subscribers") is not None and p.get("subscribers") is not None) else None
            L += ["## Channel", f"- Subscribers: {_fmt(c.get('subscribers'))}" + (f" ({d:+d} vs {p['snapshot_date']})" if d is not None else ""),
                  f"- Total views: {_fmt(c.get('total_views'))}", f"- Watch hours (365d): {_fmt(c.get('watch_hours_365d'), 1)}",
                  f"- Snapshot: {c['snapshot_date']}", ""]
        else:
            L += ["## Channel", "No channel statistics collected yet (connect YouTube and run analytics collection).", ""]
        L += ["## Videos", "| Video | Views | Avg % viewed | Likes | Subs+ |", "|---|---:|---:|---:|---:|"]
        for r in rep["videos"][:15]:
            L.append(f"| {r['title'][:60]} | {_fmt(r['views'])} | {_fmt(r['avg_view_percentage'], 1, '%')} | {_fmt(r['likes'])} | {_fmt(r['subs_gained'])} |")
        if not rep["videos"]:
            L.append("| _no analytics yet_ | | | | |")
        L += ["", f"Uploads: {rep['upload_counts']}", f"AI/API cost this month: ₹{rep['cost_inr_month']}", ""]
        L += [f"- {n}" for n in rep["notes"]]
        return "\n".join(L)

    def generate(self, kind: str = "daily", save: bool = True) -> dict:
        rep = self.build(kind)
        rep["markdown"] = self.markdown(rep)
        if save:
            d = self.cfg.path("output_dir", "reports")
            f = d / f"{kind}_{datetime.now(timezone.utc).strftime('%Y%m%d')}.md"
            f.write_text(rep["markdown"], encoding="utf-8")
            rep["path"] = str(f)
        return rep
