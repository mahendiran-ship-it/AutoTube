"""Free, licensed stock media: Pexels, Pixabay, NASA Image and Video Library.

All downloads go through the SSRF-safe downloader with host allow-lists. Every asset returns its license record.
These endpoints are used as documented by each provider; they could not be exercised live from the build sandbox
(egress is allow-listed) so they are covered by mocked-response tests — see docs/PROVIDERS.md.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.core.errors import ConfigurationError, ProviderUnavailable
from app.core.logger import get_logger
from app.utils.downloads import download_file, http_get, user_agent
from app.utils.text import tokens

log = get_logger("stock")

SPACE_TOKENS = {"space", "galaxy", "galaxies", "star", "stars", "nebula", "planet", "moon", "black", "hole", "cosmos", "cosmic",
                "universe", "astronomy", "earth", "sun", "solar", "milky", "way", "orbit", "telescope", "aurora", "eclipse",
                "asteroid", "comet", "mars", "jupiter", "saturn", "venus", "mercury", "neptune", "uranus", "nasa", "webb",
                "hubble", "supernova", "constellation", "night", "sky", "astronaut", "rocket", "satellite", "meteor", "lunar",
                "starry", "milkyway", "abstract"}


@dataclass
class Candidate:
    provider: str
    kind: str                      # video | image
    url: str                       # direct media URL
    page_url: str
    width: int
    height: int
    duration: float = 0.0
    author: str = ""
    author_url: str = ""
    title: str = ""
    tags: str = ""
    license_name: str = ""
    license_url: str = ""
    attribution_required: bool = False
    attribution_text: str = ""
    notes: str = ""
    hosts: list[str] = field(default_factory=list)

    def license_record(self) -> dict:
        return {"provider": self.provider, "license_name": self.license_name, "license_url": self.license_url,
                "author": self.author, "author_url": self.author_url, "attribution_text": self.attribution_text,
                "attribution_required": 1 if self.attribution_required else 0, "source_url": self.page_url,
                "notes": self.notes}


def relevance(c: Candidate, query: str, extra_terms: list[str] | None = None) -> float:
    """Honest relevance: candidate text must overlap with the query AND look like space content."""
    blob = set(tokens(f"{c.title} {c.tags} {re.sub(r'[^a-z0-9]+', ' ', c.page_url.lower())}"))
    q = set(tokens(query)) | set(tokens(" ".join(extra_terms or [])))
    overlap = len(blob & q)
    spacey = len(blob & SPACE_TOKENS)
    if spacey == 0:
        return 0.0
    return overlap * 1.0 + min(spacey, 3) * 0.4


class StockProvider:
    name = "stock"

    def __init__(self, cfg):
        self.cfg = cfg

    def enabled(self) -> bool:
        return False

    def _get_json(self, url: str, headers: dict | None = None, params: dict | None = None, hosts=None) -> dict:
        body, _, _ = http_get(url, timeout=20, max_bytes=2_000_000, allowed_hosts=hosts, params=params,
                              headers={"User-Agent": user_agent(self.cfg), "Accept": "application/json", **(headers or {})})
        return json.loads(body)

    def search_videos(self, query: str, n: int = 8) -> list[Candidate]:
        return []

    def search_images(self, query: str, n: int = 8) -> list[Candidate]:
        return []


class PexelsProvider(StockProvider):
    name = "pexels"
    API = "https://api.pexels.com"

    def _key(self):
        return self.cfg.env(self.cfg.get("visuals.stock.pexels.api_key_env", "PEXELS_API_KEY"))

    def enabled(self):
        return bool(self.cfg.get("visuals.stock.pexels.enabled", True)) and bool(self._key())

    def _lic(self, **kw) -> Candidate:
        return Candidate(provider="pexels", license_name="Pexels License", license_url="https://www.pexels.com/license/",
                         attribution_required=False, hosts=["pexels.com", "vimeocdn.com", "player.vimeo.com"], **kw)

    def search_videos(self, query, n=8):
        d = self._get_json(f"{self.API}/videos/search", {"Authorization": self._key()},
                           {"query": query, "orientation": "portrait", "per_page": n}, ["api.pexels.com"])
        out = []
        for v in d.get("videos", []):
            files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4" and f.get("link")]
            if not files:
                continue
            ok = [f for f in files if (f.get("width") or 0) >= 720]
            f = min(ok, key=lambda x: x.get("width") or 0) if ok else max(files, key=lambda x: x.get("width") or 0)
            user = v.get("user") or {}
            out.append(self._lic(kind="video", url=f["link"], page_url=v.get("url", ""), width=f.get("width") or 0,
                                 height=f.get("height") or 0, duration=float(v.get("duration") or 0),
                                 author=user.get("name", ""), author_url=user.get("url", ""),
                                 title=v.get("url", "").rsplit("/", 2)[-2].replace("-", " ") if v.get("url") else "",
                                 attribution_text=f"Video by {user.get('name', 'unknown')} on Pexels"))
        return out

    def search_images(self, query, n=8):
        d = self._get_json(f"{self.API}/v1/search", {"Authorization": self._key()},
                           {"query": query, "orientation": "portrait", "per_page": n}, ["api.pexels.com"])
        out = []
        for p in d.get("photos", []):
            src = p.get("src") or {}
            url = src.get("large2x") or src.get("large") or src.get("original")
            if url:
                out.append(self._lic(kind="image", url=url, page_url=p.get("url", ""), width=p.get("width") or 0,
                                     height=p.get("height") or 0, author=p.get("photographer", ""),
                                     author_url=p.get("photographer_url", ""), title=p.get("alt", "") or "",
                                     attribution_text=f"Photo by {p.get('photographer', 'unknown')} on Pexels"))
        return out


class PixabayProvider(StockProvider):
    name = "pixabay"
    API = "https://pixabay.com/api/"

    def _key(self):
        return self.cfg.env(self.cfg.get("visuals.stock.pixabay.api_key_env", "PIXABAY_API_KEY"))

    def enabled(self):
        return bool(self.cfg.get("visuals.stock.pixabay.enabled", True)) and bool(self._key())

    def _lic(self, **kw) -> Candidate:
        return Candidate(provider="pixabay", license_name="Pixabay Content License",
                         license_url="https://pixabay.com/service/license-summary/", attribution_required=False,
                         hosts=["pixabay.com"], **kw)

    def search_videos(self, query, n=8):
        d = self._get_json(self.API + "videos/", params={"key": self._key(), "q": query, "per_page": max(3, n),
                                                       "safesearch": "true"}, hosts=["pixabay.com"])
        out = []
        for h in d.get("hits", []):
            vs = h.get("videos") or {}
            f = next((vs[k] for k in ("medium", "large", "small") if vs.get(k, {}).get("url")), None)
            if f:
                out.append(self._lic(kind="video", url=f["url"], page_url=h.get("pageURL", ""), width=f.get("width") or 0,
                                     height=f.get("height") or 0, duration=float(h.get("duration") or 0),
                                     author=h.get("user", ""), tags=h.get("tags", ""),
                                     author_url=f"https://pixabay.com/users/{h.get('user', '')}-{h.get('user_id', '')}/",
                                     attribution_text=f"Video by {h.get('user', 'unknown')} on Pixabay"))
        return out

    def search_images(self, query, n=8):
        d = self._get_json(self.API, params={"key": self._key(), "q": query, "image_type": "photo", "orientation": "vertical",
                                             "per_page": max(3, n), "safesearch": "true"}, hosts=["pixabay.com"])
        return [self._lic(kind="image", url=h["largeImageURL"], page_url=h.get("pageURL", ""), width=h.get("imageWidth") or 0,
                          height=h.get("imageHeight") or 0, author=h.get("user", ""), tags=h.get("tags", ""),
                          attribution_text=f"Image by {h.get('user', 'unknown')} on Pixabay")
                for h in d.get("hits", []) if h.get("largeImageURL")]


class NasaImagesProvider(StockProvider):
    """NASA Image and Video Library (no key). Items with third-party 'secondary_creator' are skipped."""
    name = "nasa"
    API = "https://images-api.nasa.gov"

    def enabled(self):
        return bool(self.cfg.get("visuals.stock.nasa_images.enabled", True))

    def search_images(self, query, n=8):
        d = self._get_json(f"{self.API}/search", params={"q": query, "media_type": "image", "page_size": 25},
                           hosts=["images-api.nasa.gov"])
        out = []
        for it in (d.get("collection", {}).get("items") or [])[:25]:
            data = (it.get("data") or [{}])[0]
            if data.get("secondary_creator") or not data.get("nasa_id"):
                continue
            try:
                manifest = self._get_json(f"{self.API}/asset/{data['nasa_id']}", hosts=["images-api.nasa.gov"])
            except Exception:
                continue
            urls = [i.get("href", "") for i in manifest.get("collection", {}).get("items", [])]
            pick = next((u for suf in ("~large.jpg", "~medium.jpg", "~orig.jpg") for u in urls if u.lower().endswith(suf)), None)
            if not pick:
                continue
            page = f"https://images.nasa.gov/details/{data['nasa_id']}"
            out.append(Candidate(provider="nasa", kind="image", url=pick.replace("http://", "https://"), page_url=page,
                                 width=0, height=0, author=data.get("center", "NASA"), title=data.get("title", ""),
                                 tags=" ".join(data.get("keywords") or []), license_name="NASA Media Usage Guidelines",
                                 license_url="https://www.nasa.gov/nasa-brand-center/images-and-media/",
                                 attribution_required=True, attribution_text=f"Image credit: NASA ({data.get('title', '')[:60]})",
                                 notes="Generally not copyrighted; must not imply NASA endorsement.",
                                 hosts=["nasa.gov"]))
            if len(out) >= n:
                break
        return out


class StockManager:
    def __init__(self, cfg, repo, providers: list[StockProvider] | None = None, session=None):
        self.cfg, self.repo, self.session = cfg, repo, session
        self.providers = providers if providers is not None else [PexelsProvider(cfg), PixabayProvider(cfg), NasaImagesProvider(cfg)]

    def active(self) -> list[StockProvider]:
        return [p for p in self.providers if p.enabled()]

    def _pick(self, cands: list[Candidate], query: str, extra: list[str], video_id: int | None, want_dur: float = 0) -> list[Candidate]:
        scored = []
        for c in cands:
            r = relevance(c, query, extra)
            if r <= 0:
                continue
            if self.cfg.get("visuals.avoid_reusing_assets", True) and self.repo.asset_source_used(c.page_url or c.url, video_id):
                continue
            s = r
            if c.kind == "video":
                s += 0.5 if c.height >= c.width else 0
                s += 0.5 if want_dur and c.duration >= want_dur else (-1.0 if c.duration and c.duration < 3 else 0)
            scored.append((s, c))
        return [c for _, c in sorted(scored, key=lambda t: -t[0])]

    def find(self, kind: str, query: str, extra_terms: list[str], video_id: int | None, want_dur: float = 0) -> list[Candidate]:
        limit = int(self.cfg.get("visuals.max_stock_candidates", 8))
        out: list[Candidate] = []
        for p in self.active():
            try:
                found = p.search_videos(query, limit) if kind == "video" else p.search_images(query, limit)
                out += found
            except Exception as e:
                log.warning("stock provider %s failed: %s", p.name, e)
        return self._pick(out, query, extra_terms, video_id, want_dur)

    def download(self, c: Candidate, dest_dir: Path, name: str) -> Path:
        prefixes = ("video/",) if c.kind == "video" else ("image/",)
        return download_file(c.url, dest_dir, name, max_bytes=150_000_000 if c.kind == "video" else 25_000_000,
                             allowed_hosts=c.hosts or None, content_type_prefixes=prefixes, session=self.session,
                             headers={"User-Agent": user_agent(self.cfg)})
