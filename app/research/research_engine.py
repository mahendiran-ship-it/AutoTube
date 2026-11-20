"""Research engine: gather multiple sources for the selected topic, extract facts, persist a record.

Honest by construction: with an LLM, facts are extracted from the fetched documents only. Without one,
an *extractive* fallback copies sentences verbatim from the sources (no paraphrase, no invention).
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote, urlparse

from app.ai.base import render_prompt
from app.core.logger import get_logger
from app.research.feed_collector import FeedCollector
from app.utils.downloads import fetch_text, html_to_text, http_get, user_agent
from app.utils.text import keywords, numbers_in, split_sentences, title_similarity, tokens, truncate

log = get_logger("research")

_HEDGE_RE = re.compile(r"\b(may|might|could|possibly|perhaps|suggests?|suspect|believed?|thought to|hypothes|theor|"
                       r"unclear|unknown|appears?|likely|candidate|potential|proposed|estimated)\b", re.I)


def classify_sentence(s: str) -> str:
    if re.search(r"\b(theor(y|ies)|hypothes[ie]s)\b", s, re.I):
        return "theory"
    if _HEDGE_RE.search(s):
        return "speculation"
    return "confirmed"


class ResearchEngine:
    def __init__(self, cfg, repo, ai=None, fetcher=None, feed_collector: FeedCollector | None = None):
        self.cfg, self.repo, self.ai = cfg, repo, ai
        self._fetcher = fetcher or self._default_fetcher
        self.feed_collector = feed_collector

    # ---- source gathering ---------------------------------------------
    def _default_fetcher(self, url: str) -> str:
        html, _ = fetch_text(url, timeout=float(self.cfg.get("research.http_timeout_seconds", 20)),
                             max_bytes=1_500_000, headers={"User-Agent": user_agent(self.cfg)})
        return html_to_text(html, int(self.cfg.get("research.max_page_chars", 12000)))

    def _wikipedia(self, topic: dict) -> dict | None:
        if not self.cfg.get("research.wikipedia_enabled", True):
            return None
        q = " ".join(keywords(topic["title"], 4)) or topic["title"]
        try:
            body, _, _ = http_get(
                "https://en.wikipedia.org/w/api.php", timeout=15, max_bytes=300_000, allowed_hosts=["wikipedia.org"],
                params={"action": "opensearch", "search": q, "limit": 1, "format": "json"},
                headers={"User-Agent": user_agent(self.cfg)})
            res = json.loads(body)
            if not res[1]:
                return None
            title = res[1][0]
            if title_similarity(title, topic["title"]) < 0.15 and not (set(tokens(title)) & set(tokens(topic["title"]))):
                return None
            slug = quote(title.replace(" ", "_"), safe="")
            body, _, _ = http_get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{slug}", timeout=15,
                                  max_bytes=300_000, allowed_hosts=["wikipedia.org"],
                                  headers={"User-Agent": user_agent(self.cfg)})
            d = json.loads(body)
            if not d.get("extract"):
                return None
            return {"title": d.get("title", title), "text": d["extract"], "reliability": "medium",
                    "url": d.get("content_urls", {}).get("desktop", {}).get("page", f"https://en.wikipedia.org/wiki/{slug}"),
                    "publisher": "Wikipedia"}
        except Exception as e:
            log.debug("wikipedia lookup failed: %s", e)
            return None

    def gather(self, topic: dict) -> list[dict]:
        docs: list[dict] = []
        seen_urls: set[str] = set()
        # 1) the primary item
        if topic.get("url") and "wikipedia.org" not in topic["url"]:
            try:
                text = self._fetcher(topic["url"])
                if len(text) > 200:
                    sig = topic.get("signal_data") or {}
                    docs.append({"title": topic["title"], "url": topic["url"], "text": text,
                                 "reliability": (sig.get("reliability") if isinstance(sig, dict) else None) or "medium",
                                 "publisher": topic.get("source_name") or urlparse(topic["url"]).netloc})
                    seen_urls.add(topic["url"])
            except Exception as e:
                log.warning("could not fetch primary source %s: %s", topic["url"], e)
        if not docs and topic.get("summary"):
            docs.append({"title": topic["title"], "url": topic.get("url") or "", "text": topic["summary"],
                         "reliability": "medium", "publisher": topic.get("source_name") or "feed summary",
                         "note": "feed summary only (full page unavailable)"})
        # 2) corroborating feed items about the same story (other publishers)
        sig = topic.get("signal_data") or {}
        try:
            if self.feed_collector:
                items, _ = self.feed_collector.collect()
                for it in items:
                    if it.link in seen_urls or it.source_name == topic.get("source_name"):
                        continue
                    if title_similarity(it.title, topic["title"]) >= 0.4:
                        try:
                            text = self._fetcher(it.link)
                        except Exception:
                            text = it.summary
                        if len(text) > 120:
                            docs.append({"title": it.title, "url": it.link, "text": text, "reliability": it.reliability,
                                         "publisher": it.source_name})
                            seen_urls.add(it.link)
                    if len(docs) >= 4:
                        break
        except Exception as e:
            log.debug("corroboration step failed: %s", e)
        # 3) encyclopedia background
        wiki = self._wikipedia(topic)
        if wiki:
            docs.append(wiki)
        for i, d in enumerate(docs, 1):
            d["id"] = f"S{i}"
        return docs

    # ---- fact extraction ----------------------------------------------
    def _extractive(self, topic: dict, docs: list[dict]) -> dict:
        kw = set(keywords(topic["title"] + " " + (topic.get("summary") or ""), 8))
        facts, seen = [], set()
        for d in docs:
            for s in split_sentences(d["text"]):
                if not (40 <= len(s) <= 260) or s.lower() in seen:
                    continue
                if not (set(tokens(s)) & kw):
                    continue
                if re.search(r"(subscribe|newsletter|cookie|advertis|copyright|follow us|sign up|click here)", s, re.I):
                    continue
                seen.add(s.lower())
                facts.append({"claim": s, "status": classify_sentence(s), "sources": [d["id"]], "numbers": numbers_in(s),
                              "verbatim": True})
                if len(facts) >= 10:
                    break
            if len(facts) >= 10:
                break
        return {"summary": truncate(docs[0]["text"], 300) if docs else "", "facts": facts, "open_questions": [],
                "key_terms": sorted(kw)[:8], "hedging_needed": any(f["status"] != "confirmed" for f in facts)}

    def extract(self, topic: dict, docs: list[dict], video_id: int | None = None) -> tuple[dict, str]:
        if self.ai is not None:
            try:
                payload = [{"id": d["id"], "publisher": d.get("publisher"), "text": truncate(d["text"], 3500)} for d in docs]
                prompt = render_prompt("research_extract", topic_title=topic["title"], sources=payload)
                data, resp = self.ai.generate_json(prompt, kind="research_extract", video_id=video_id, temperature=0.2)
                facts = [f for f in (data.get("facts") or []) if isinstance(f, dict) and f.get("claim")]
                if facts:
                    ids = {d["id"] for d in docs}
                    for f in facts:
                        f["sources"] = [s for s in (f.get("sources") or []) if s in ids]
                        f["status"] = f.get("status") if f.get("status") in ("confirmed", "theory", "speculation", "uncertain") else "uncertain"
                    facts = [f for f in facts if f["sources"]]      # un-sourced facts are dropped
                    if facts:
                        data["facts"] = facts
                        return data, f"llm:{resp.provider}"
            except Exception as e:
                log.warning("LLM fact extraction unavailable (%s) — using extractive fallback", e)
        return self._extractive(topic, docs), "extractive"

    def research(self, topic: dict, video_id: int, run_dir=None) -> dict:
        docs = self.gather(topic)
        if not docs:
            raise RuntimeError("no source documents could be retrieved for this topic")
        notes, method = self.extract(topic, docs, video_id)
        if not notes.get("facts"):
            raise RuntimeError("no verifiable facts could be extracted from the sources")
        distinct = {d.get("publisher") for d in docs}
        min_sources = int(self.cfg.get("research.min_sources_recommended", 2))
        warnings = []
        if len(distinct) < min_sources:
            warnings.append(f"only {len(distinct)} distinct source(s) found (recommended {min_sources}); "
                            "claims rely on limited corroboration")
        if all(d.get("note") for d in docs):
            warnings.append("only feed summaries were available")
        sources = [{"id": d["id"], "title": d["title"], "url": d["url"], "publisher": d.get("publisher"),
                    "reliability": d.get("reliability"), "chars": len(d["text"])} for d in docs]
        record = {"topic": {"id": topic["id"], "title": topic["title"]}, "method": method, "warnings": warnings,
                  "summary": notes.get("summary", ""), "facts": notes["facts"],
                  "open_questions": notes.get("open_questions", []), "key_terms": notes.get("key_terms", []),
                  "sources": sources, "excerpts": {d["id"]: truncate(d["text"], 2500) for d in docs}}
        self.repo.save_research(video_id, topic["id"], record["summary"], record, sources, method)
        if run_dir:
            from pathlib import Path
            p = Path(run_dir)
            p.mkdir(parents=True, exist_ok=True)
            (p / "research.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        return record
