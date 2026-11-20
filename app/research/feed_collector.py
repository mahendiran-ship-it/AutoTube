"""RSS/Atom collection. Each source is isolated: one failing feed never stops the others."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from app.core.logger import get_logger
from app.utils.downloads import http_get, strip_html, user_agent
from app.utils.text import truncate

log = get_logger("feeds")

ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
DC = "{http://purl.org/dc/elements/1.1/}"


@dataclass
class FeedItem:
    title: str
    link: str
    summary: str
    published: datetime | None
    source_key: str
    source_name: str
    reliability: str = "medium"
    categories_hint: list[str] = field(default_factory=list)


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def parse_feed(xml_bytes: bytes, source: dict) -> list[FeedItem]:
    """Parse RSS 2.0 / Atom. Rejects DTD/entity declarations (XML-bomb protection)."""
    head = xml_bytes[:4096].lower()
    if b"<!doctype" in head and b"<!entity" in xml_bytes.lower():
        raise ValueError("feed contains entity declarations — rejected")
    if b"<!entity" in xml_bytes.lower():
        raise ValueError("feed contains entity declarations — rejected")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise ValueError(f"invalid XML: {e}") from e
    items: list[FeedItem] = []
    common = dict(source_key=source["id"], source_name=source.get("name", source["id"]),
                  reliability=source.get("reliability", "medium"), categories_hint=list(source.get("categories") or []))
    for it in root.iter("item"):                      # RSS
        title = strip_html(_text(it.find("title")))
        link = _text(it.find("link"))
        desc = _text(it.find(f"{CONTENT}encoded")) or _text(it.find("description"))
        pub = _parse_date(_text(it.find("pubDate")) or _text(it.find(f"{DC}date")))
        if title and link:
            items.append(FeedItem(title=title, link=link, summary=truncate(strip_html(desc), 1200),
                                  published=pub, **common))
    for it in root.iter(f"{ATOM}entry"):              # Atom
        title = strip_html(_text(it.find(f"{ATOM}title")))
        link_el = next((l for l in it.findall(f"{ATOM}link") if l.get("rel") in (None, "alternate")), None)
        link = (link_el.get("href") if link_el is not None else "") or ""
        desc = _text(it.find(f"{ATOM}summary")) or _text(it.find(f"{ATOM}content"))
        pub = _parse_date(_text(it.find(f"{ATOM}published")) or _text(it.find(f"{ATOM}updated")))
        if title and link:
            items.append(FeedItem(title=title, link=link, summary=truncate(strip_html(desc), 1200),
                                  published=pub, **common))
    return items


class FeedCollector:
    def __init__(self, cfg, repo, fetch=None):
        self.cfg, self.repo = cfg, repo
        self._fetch = fetch or self._default_fetch

    def _default_fetch(self, url: str) -> bytes:
        body, _, _ = http_get(url, timeout=float(self.cfg.get("research.http_timeout_seconds", 20)),
                              max_bytes=3_000_000, headers={"User-Agent": user_agent(self.cfg),
                                                            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"})
        return body

    def feeds(self) -> list[dict]:
        return [f for f in (self.cfg.get("sources.feeds") or []) if f.get("id") and f.get("url")]

    def sync_sources(self) -> dict[str, int]:
        """Mirror configured feeds into the `sources` table; returns {feed_id: db_id}."""
        ids = {}
        for f in self.feeds():
            ids[f["id"]] = self.repo.upsert_source(f["id"], f.get("name", f["id"]), f["url"],
                                                   f.get("reliability", "medium"), f.get("enabled", True),
                                                   categories=f.get("categories"))
        return ids

    def collect(self) -> tuple[list[FeedItem], list[dict]]:
        ids = self.sync_sources()
        items: list[FeedItem] = []
        report: list[dict] = []
        for f in self.feeds():
            if not f.get("enabled", True):
                report.append({"id": f["id"], "name": f.get("name"), "status": "disabled", "items": 0})
                continue
            try:
                parsed = parse_feed(self._fetch(f["url"]), f)
                items.extend(parsed)
                self.repo.record_source_fetch(ids[f["id"]], True, len(parsed))
                report.append({"id": f["id"], "name": f.get("name"), "status": "ok", "items": len(parsed)})
            except Exception as e:   # isolate EVERY failure
                msg = f"{type(e).__name__}: {e}"[:300]
                log.warning("source %s failed: %s", f["id"], msg)
                self.repo.record_source_fetch(ids[f["id"]], False, 0, msg)
                report.append({"id": f["id"], "name": f.get("name"), "status": "error", "items": 0, "error": msg})
        return items, report

    def check_sources(self) -> list[dict]:
        """For `main.py check-sources`: probe every configured feed (enabled or not)."""
        out = []
        for f in self.feeds():
            try:
                n = len(parse_feed(self._fetch(f["url"]), f))
                out.append({"id": f["id"], "url": f["url"], "ok": n > 0, "items": n,
                            "detail": "ok" if n else "parsed but 0 items"})
            except Exception as e:
                out.append({"id": f["id"], "url": f["url"], "ok": False, "items": 0,
                            "detail": re.sub(r"\s+", " ", f"{type(e).__name__}: {e}")[:200]})
        return out
