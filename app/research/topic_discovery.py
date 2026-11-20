"""Topic discovery: feeds (research signals) + real Wikimedia pageview trends (measured, never guessed)."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from app.core.logger import get_logger
from app.research.feed_collector import FeedCollector, FeedItem
from app.utils.downloads import http_get, user_agent
from app.utils.text import (fingerprint, jaccard, keywords, normalize, title_case_clean, title_similarity,
                            tokens, truncate)

log = get_logger("discovery")
RELIABILITY_RANK = {"high": 3, "medium": 2, "low": 1}


class WikipediaTrends:
    """Measured attention: Wikimedia daily pageviews for a watchlist (REST API, no key needed).

    ratio = mean(last 3 complete days) / mean(previous 14 days). Only this measurement may
    make a topic a "trend"."""

    def __init__(self, cfg, fetch=None, sleep=time.sleep):
        self.cfg = cfg
        self._sleep = sleep
        self._fetch = fetch or self._default_fetch

    def _default_fetch(self, url: str) -> dict:
        body, _, _ = http_get(url, timeout=15, max_bytes=500_000, allowed_hosts=["wikimedia.org", "wikipedia.org"],
                              headers={"User-Agent": user_agent(self.cfg), "Accept": "application/json"})
        return json.loads(body)

    def enabled(self) -> bool:
        return bool(self.cfg.get("research.trend.enabled", True)) and \
            bool(self.cfg.get("sources.trend_sources.wikipedia_pageviews.enabled", False))

    def measure(self, today: datetime | None = None) -> list[dict]:
        if not self.enabled():
            return []
        cfgt = self.cfg.get("sources.trend_sources.wikipedia_pageviews", {})
        project = cfgt.get("project", "en.wikipedia")
        end = (today or datetime.now(timezone.utc)) - timedelta(days=1)       # last complete day
        start = end - timedelta(days=17)
        out = []
        for article in cfgt.get("watchlist", []):
            slug = quote(article.replace(" ", "_"), safe="")
            url = (f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/{project}/all-access/user/"
                   f"{slug}/daily/{start:%Y%m%d}/{end:%Y%m%d}")
            try:
                data = self._fetch(url)
                views = [int(i["views"]) for i in data.get("items", [])]
            except Exception as e:
                log.debug("pageviews failed for %s: %s", article, e)
                continue
            finally:
                self._sleep(0.15)
            if len(views) < 10:
                continue
            recent, base = views[-3:], views[:-3][-14:]
            base_avg = sum(base) / len(base) if base else 0
            recent_avg = sum(recent) / len(recent)
            ratio = recent_avg / base_avg if base_avg > 0 else 0.0
            out.append({"article": article, "recent_avg": round(recent_avg, 1), "baseline_avg": round(base_avg, 1),
                        "ratio": round(ratio, 2), "days_measured": len(views), "source": "wikimedia_pageviews",
                        "window_end": f"{end:%Y-%m-%d}"})
        return sorted(out, key=lambda r: -r["ratio"])


class TopicDiscovery:
    def __init__(self, cfg, repo, collector: FeedCollector, trends: WikipediaTrends | None = None, fetch_json=None):
        self.cfg, self.repo, self.collector, self.trends = cfg, repo, collector, trends
        self._fetch_json = fetch_json

    # ---- helpers ------------------------------------------------------
    def categorize(self, text: str, hint: list[str] | None = None) -> str | None:
        """Subject categories win over 'secondary' ones (instruments/framing such as missions)."""
        t = (text or "").lower()
        secondary = set(self.cfg.get("channel.secondary_categories") or [])
        scores: dict[str, int] = {}
        for cat, words in (self.cfg.get("channel.categories") or {}).items():
            n = sum(min(2, t.count(w.lower())) for w in words)       # cap repeats so one word can't dominate
            if hint and cat in hint:
                n += 1
            if n:
                scores[cat] = n
        for pool in ([c for c in scores if c not in secondary], [c for c in scores if c in secondary]):
            if pool:
                return max(pool, key=lambda c: scores[c])
        return None

    def is_blocked(self, text: str) -> str | None:
        t = (text or "").lower()
        for term in self.cfg.get("channel.blocked_terms") or []:
            if term.lower() in t:
                return term
        return None

    def _cluster(self, items: list[FeedItem]) -> list[tuple[FeedItem, list[str]]]:
        """Group items about the same story; the most reliable is the representative."""
        items = sorted(items, key=lambda i: (-RELIABILITY_RANK.get(i.reliability, 1),
                                             i.published or datetime.min.replace(tzinfo=timezone.utc)), reverse=False)
        items = sorted(items, key=lambda i: -RELIABILITY_RANK.get(i.reliability, 1))
        clusters: list[tuple[FeedItem, set[str]]] = []
        for it in items:
            placed = False
            for rep, srcs in clusters:
                if title_similarity(rep.title, it.title) >= 0.5 or \
                        jaccard(keywords(rep.title + " " + rep.summary, 6), keywords(it.title + " " + it.summary, 6)) >= 0.5:
                    if it.source_name != rep.source_name:
                        srcs.add(it.source_name)
                    placed = True
                    break
            if not placed:
                clusters.append((it, set()))
        return [(rep, sorted(s)) for rep, s in clusters]

    # ---- main ---------------------------------------------------------
    def discover(self) -> dict:
        max_age = int(self.cfg.get("research.max_item_age_days", 21))
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age)
        items, source_report = self.collector.collect()
        fresh = [i for i in items if (i.published is None or i.published >= cutoff)]
        trend_rows = self.trends.measure() if self.trends else []
        threshold = float(self.cfg.get("research.trend.spike_ratio_threshold", 1.6))
        spiking = [t for t in trend_rows if t["ratio"] >= threshold and t["recent_avg"] >= 100]

        added, blocked, dup = 0, 0, 0
        for rep, also in self._cluster(fresh):
            title = title_case_clean(rep.title)
            if len(title) < 12:
                continue
            hit = self.is_blocked(title + " " + rep.summary)
            if hit:
                blocked += 1
                continue
            signal_type, signal = "research_signal", {"also_covered_by": also, "coverage": 1 + len(also),
                                                       "reliability": rep.reliability,
                                                       "label": "research signal (feed coverage), not a measured trend"}
            ttoks = set(tokens(title))
            for t in spiking:
                if ttoks & set(tokens(t["article"])) and len(set(tokens(t["article"])) - ttoks) <= 1:
                    signal_type, signal = "trend", {**signal, "trend": t}
                    break
            fp = fingerprint(title)
            sid = self.repo.db.one("SELECT id FROM sources WHERE source_key=?", (rep.source_key,))
            tid = self.repo.add_topic(
                title=title, normalized_title=normalize(title), fingerprint=fp, summary=truncate(rep.summary, 900),
                url=rep.link, source_id=sid["id"] if sid else None, source_name=rep.source_name,
                signal_type=signal_type, signal_data=signal,
                category=self.categorize(title + " " + rep.summary, rep.categories_hint),
                keywords=keywords(title + " " + rep.summary, 8),
                published_at=rep.published.isoformat() if rep.published else None)
            if tid:
                added += 1
            else:
                dup += 1

        trend_topics = 0
        for t in spiking[:5]:   # evergreen topics whose attention is measurably rising
            title = t["article"]
            fp = fingerprint("trend " + title)
            summary, url = self._wiki_summary(title)
            tid = self.repo.add_topic(
                title=title, normalized_title=normalize(title), fingerprint=fp, summary=truncate(summary, 900),
                url=url, source_name="Wikipedia (pageview trend)", signal_type="trend",
                signal_data={"trend": t, "label": "measured Wikimedia pageview spike"},
                category=self.categorize(title + " " + summary), keywords=keywords(title + " " + summary, 8))
            if tid:
                trend_topics += 1
        return {"items_seen": len(items), "fresh": len(fresh), "new_topics": added, "already_known": dup,
                "blocked": blocked, "trend_topics": trend_topics, "trends_measured": len(trend_rows),
                "trends_spiking": len(spiking), "sources": source_report}

    def _wiki_summary(self, title: str) -> tuple[str, str]:
        slug = quote(title.replace(" ", "_"), safe="")
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{slug}"
        try:
            fetch = self._fetch_json or (lambda u: json.loads(http_get(
                u, timeout=15, max_bytes=300_000, allowed_hosts=["wikipedia.org"],
                headers={"User-Agent": user_agent(self.cfg)})[0]))
            d = fetch(url)
            return d.get("extract", ""), (d.get("content_urls", {}).get("desktop", {}).get("page")
                                          or f"https://en.wikipedia.org/wiki/{slug}")
        except Exception:
            return "", f"https://en.wikipedia.org/wiki/{slug}"
