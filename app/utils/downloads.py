"""Safe HTTP fetching/downloading with SSRF protection, size limits and sanitised names."""
from __future__ import annotations

import os
import re
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from app.core.errors import ProviderUnavailable, UnsafeURL
from app.utils.validators import sanitize_filename, validate_url

MAX_REDIRECTS = 5


def user_agent(cfg=None) -> str:
    contact = ""
    if cfg is not None:
        contact = cfg.env(cfg.get("research.contact_env", "AUTOTUBE_CONTACT"), "") or ""
    base = "AutoTubeAI/1.0 (+https://github.com/ ; research bot)"
    return f"{base} {contact}".strip()


def http_get(url: str, *, timeout: float = 20, max_bytes: int = 2_000_000, allowed_hosts=None,
             headers: dict | None = None, params: dict | None = None, session: requests.Session | None = None,
             check_dns: bool = True, allow_http: bool = False) -> tuple[bytes, str, str]:
    """GET with manual redirect handling (every hop re-validated). Returns (body, content_type, final_url)."""
    sess = session or requests
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        validate_url(current, allowed_hosts=allowed_hosts, check_dns=check_dns, allow_http=allow_http)
        try:
            r = sess.get(current, headers=headers or {}, params=params if current == url else None,
                         timeout=timeout, stream=True, allow_redirects=False)
        except requests.RequestException as e:
            raise ProviderUnavailable(f"request failed for {urlparse(current).netloc}: {e}") from e
        try:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                current = urljoin(current, r.headers["Location"])
                continue
            if r.status_code >= 400:
                raise ProviderUnavailable(f"HTTP {r.status_code} from {urlparse(current).netloc}")
            clen = r.headers.get("Content-Length")
            if clen and clen.isdigit() and int(clen) > max_bytes:
                raise ProviderUnavailable(f"response too large ({clen} bytes > {max_bytes})")
            chunks, total = [], 0
            for chunk in r.iter_content(65536):
                total += len(chunk)
                if total > max_bytes:
                    raise ProviderUnavailable(f"response exceeded {max_bytes} bytes")
                chunks.append(chunk)
            return b"".join(chunks), r.headers.get("Content-Type", ""), current
        finally:
            r.close()
    raise ProviderUnavailable("too many redirects")


def fetch_text(url: str, **kw) -> tuple[str, str]:
    body, ctype, final = http_get(url, **kw)
    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", ctype or "", re.I)
    if m:
        charset = m.group(1)
    return body.decode(charset, errors="replace"), final


def download_file(url: str, dest_dir: Path, filename_hint: str, *, max_bytes: int = 120_000_000,
                  allowed_hosts=None, content_type_prefixes: tuple[str, ...] = ("video/", "image/"),
                  timeout: float = 60, session=None, headers: dict | None = None) -> Path:
    """Download to dest_dir/sanitised-name atomically. Rejects unexpected content types."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    body, ctype, final = http_get(url, timeout=timeout, max_bytes=max_bytes, allowed_hosts=allowed_hosts,
                                  session=session, headers=headers)
    base_ctype = (ctype or "").split(";")[0].strip().lower()
    if content_type_prefixes and not any(base_ctype.startswith(p) for p in content_type_prefixes):
        raise UnsafeURL(f"unexpected content-type '{base_ctype}'")
    name = sanitize_filename(filename_hint)
    if "." not in name:
        ext = {"video/mp4": ".mp4", "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(base_ctype, "")
        name += ext
    target = dest_dir / name
    fd, tmp = tempfile.mkstemp(dir=dest_dir, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return target


class _TextExtractor(HTMLParser):
    KEEP = {"p", "h1", "h2", "h3", "li", "blockquote"}
    SKIP = {"script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._keep = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.KEEP:
            self._keep += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.KEEP and self._keep:
            self._keep -= 1
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip and self._keep and data.strip():
            self.parts.append(data.strip() + " ")


def html_to_text(html: str, max_chars: int = 12000) -> str:
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:
        pass
    text = re.sub(r"[ \t]+", " ", "".join(p.parts))
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:max_chars]


def strip_html(fragment: str) -> str:
    """Strip tags from a short HTML fragment (feed summaries)."""
    p = _TextExtractor()
    p.KEEP = set()  # collect everything
    p2 = HTMLParser(convert_charrefs=True)
    out: list[str] = []
    p2.handle_data = lambda d: out.append(d)  # type: ignore[assignment]
    try:
        p2.feed(fragment or "")
    except Exception:
        return re.sub(r"<[^>]+>", " ", fragment or "")
    return re.sub(r"\s+", " ", " ".join(out)).strip()
