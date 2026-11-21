"""Performance optimizer: learns from REAL analytics which topic categories perform best.

Only one thing is auto-applied: small, bounded `topic_weights` per category (used by TopicSelector).
Everything else (hook style, length, upload time...) is surfaced as a recommendation, never applied silently.
No conclusion is drawn from fewer than `optimizer.min_samples_for_conclusion` videos per category.
"""
from __future__ import annotations

import math
from statistics import mean

from app.ai.base import render_prompt
from app.core.logger import get_logger

log = get_logger("optimizer")


class PerformanceOptimizer:
    def __init__(self, cfg, repo, ai=None):
        self.cfg, self.repo, self.ai = cfg, repo, ai

    def _min_n(self) -> int:
        return int(self.cfg.get("optimizer.min_samples_for_conclusion", 5))

    def collect_samples(self) -> list[dict]:
        out = []
        for a in self.repo.latest_analytics():
            if not a.get("video_id") or a.get("views") is None:
                continue
            v = self.repo.get_video(a["video_id"])
            if not v or v.get("is_smoke"):
                continue
            topic = self.repo.get("topics", v["topic_id"]) if v.get("topic_id") else None
            out.append({"video_id": v["id"], "title": v["title"], "category": (topic or {}).get("category") or "unknown",
                        "views": a["views"] or 0, "avg_view_percentage": a.get("avg_view_percentage"),
                        "duration": v.get("duration_seconds"), "subs_gained": a.get("subs_gained") or 0})
        return out

    @staticmethod
    def _score(s: dict, mean_views: float, mean_pct: float) -> float:
        vr = (s["views"] / mean_views) if mean_views > 0 else 1.0
        pr = (s["avg_view_percentage"] / mean_pct) if (s.get("avg_view_percentage") is not None and mean_pct > 0) else vr
        clip = lambda x: max(0.25, min(3.0, x))
        return 0.6 * clip(pr) + 0.4 * clip(vr)       # retention counts more than raw views

    def analyze(self) -> dict:
        samples = self.collect_samples()
        n_min = self._min_n()
        res: dict = {"samples": len(samples), "min_samples": n_min, "categories": {}, "sufficient": False}
        if not samples:
            res["note"] = "No analytics yet. Publish videos and run analytics collection first."
            return res
        mv = mean(s["views"] for s in samples)
        pcts = [s["avg_view_percentage"] for s in samples if s["avg_view_percentage"] is not None]
        mp = mean(pcts) if pcts else 0.0
        by: dict[str, list[dict]] = {}
        for s in samples:
            by.setdefault(s["category"], []).append(s)
        for cat, rows in by.items():
            sc = mean(self._score(r, mv, mp) for r in rows)
            res["categories"][cat] = {"n": len(rows), "mean_views": round(mean(r["views"] for r in rows), 1),
                                      "mean_view_pct": round(mean([r["avg_view_percentage"] for r in rows if r["avg_view_percentage"] is not None]), 1)
                                      if any(r["avg_view_percentage"] is not None for r in rows) else None,
                                      "relative_score": round(sc, 3), "enough_data": len(rows) >= n_min}
        res["channel_mean_views"], res["channel_mean_view_pct"] = round(mv, 1), round(mp, 1)
        res["sufficient"] = any(c["enough_data"] for c in res["categories"].values())
        # length bucket observation (recommendation material only)
        short = [s for s in samples if s["duration"] and s["duration"] < 40]
        long_ = [s for s in samples if s["duration"] and s["duration"] >= 40]
        if len(short) >= n_min and len(long_) >= n_min:
            res["length_comparison"] = {"under_40s_mean_views": round(mean(s["views"] for s in short), 1),
                                        "over_40s_mean_views": round(mean(s["views"] for s in long_), 1)}
        return res

    def propose_weights(self, stats: dict) -> dict[str, float]:
        lo, hi = float(self.cfg.get("optimizer.weight_min", 0.8)), float(self.cfg.get("optimizer.weight_max", 1.25))
        step = float(self.cfg.get("optimizer.max_weight_step", 0.05))
        current = self.repo.get_setting("topic_weights", {}) or {}
        new = {}
        for cat, c in stats["categories"].items():
            if not c["enough_data"] or cat == "unknown":
                continue
            target = max(lo, min(hi, 1.0 + 0.5 * (c["relative_score"] - 1.0)))
            cur = float(current.get(cat, 1.0))
            nxt = cur + max(-step, min(step, target - cur))
            new[cat] = round(max(lo, min(hi, nxt)), 3)
        return new

    def run(self, apply: bool | None = None) -> dict:
        apply = bool(self.cfg.get("optimizer.auto_apply_topic_weights", True)) if apply is None else apply
        stats = self.analyze()
        result = {"stats": stats, "weights": {}, "applied": False, "recommendations": []}
        if not stats["sufficient"]:
            result["note"] = (f"Not enough data to adjust anything (need >= {self._min_n()} analysed videos in a category). "
                              "Topic weights are unchanged.")
            return result
        new = self.propose_weights(stats)
        current = self.repo.get_setting("topic_weights", {}) or {}
        changed = {k: v for k, v in new.items() if abs(v - float(current.get(k, 1.0))) > 1e-9}
        result["weights"] = new
        if changed:
            merged = {**current, **new}
            self.repo.add_strategy_change("topic_weights", {"from": {k: current.get(k, 1.0) for k in changed}, "to": changed},
                                          "Category performance relative to channel average (retention 60%, views 40%)",
                                          evidence=stats, auto_applied=apply, status="applied" if apply else "proposed")
            if apply:
                self.repo.set_setting("topic_weights", merged)
                result["applied"] = True
        result["recommendations"] = self._recommendations(stats)
        return result

    def _recommendations(self, stats: dict) -> list[dict]:
        if self.ai is None:
            return []
        try:
            prompt = render_prompt("optimizer", channel_name=self.cfg.get("channel.name", "the channel"),
                                   stats=stats, min_samples=str(self._min_n()))
            data, _ = self.ai.generate_json(prompt, kind="optimizer")
            recs = [r for r in (data.get("recommendations") or []) if isinstance(r, dict) and r.get("insight")][:6]
            for r in recs:
                if stats["samples"] < self._min_n():
                    r["confidence"] = "low"
            return recs
        except Exception as e:
            log.info("no AI recommendations (%s)", type(e).__name__)
            return []
