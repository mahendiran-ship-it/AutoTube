"""Input validation helpers: URLs (SSRF-aware), filenames, paths."""
from __future__ import annotations

import ipaddress
import re
import socket
import unicodedata
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse

from app.core.errors import UnsafeURL

_FILENAME_BAD = re.compile(r"[^A-Za-z0-9._-]+")
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def sanitize_filename(name: str, max_len: int = 80, default: str = "file") -> str:
    """Return a filesystem-safe filename (no separators, no traversal, no reserved names)."""
    name = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii")
    name = name.replace("/", "_").replace("\\", "_")
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = _FILENAME_BAD.sub("_", stem).strip("._-") or default
    ext = _FILENAME_BAD.sub("", ext)[:8]
    if stem.lower() in _WINDOWS_RESERVED:
        stem = f"_{stem}"
    stem = stem[: max_len - (len(ext) + 1 if ext else 0)]
    return f"{stem}.{ext}" if ext else stem


def slugify(text: str, max_len: int = 48) -> str:
    s = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:max_len].strip("-") or "untitled"


def is_within(base: Path | str, candidate: Path | str) -> bool:
    base_r = Path(base).resolve()
    try:
        Path(candidate).resolve().relative_to(base_r)
        return True
    except ValueError:
        return False


def _is_public_ip(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast
                or a.is_reserved or a.is_unspecified)


def default_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None)
    return list({i[4][0] for i in infos})


def validate_url(url: str, allowed_hosts: Iterable[str] | None = None, allow_http: bool = False,
                 resolver: Callable[[str], list[str]] | None = None, check_dns: bool = True) -> str:
    """Raise UnsafeURL unless `url` is a public http(s) URL (optionally on an allow-list)."""
    if not isinstance(url, str) or not url.strip():
        raise UnsafeURL("empty URL")
    p = urlparse(url.strip())
    if p.scheme not in (("https", "http") if allow_http else ("https",)):
        raise UnsafeURL(f"scheme '{p.scheme}' not allowed")
    if p.username or p.password:
        raise UnsafeURL("credentials in URL not allowed")
    host = (p.hostname or "").lower()
    if not host:
        raise UnsafeURL("missing host")
    if allowed_hosts:
        ok = any(host == h.lower() or host.endswith("." + h.lower()) for h in allowed_hosts)
        if not ok:
            raise UnsafeURL(f"host '{host}' not in allow-list")
    try:
        ipaddress.ip_address(host)
        if not _is_public_ip(host):
            raise UnsafeURL(f"non-public address {host}")
        return url.strip()
    except ValueError:
        pass
    if host in {"localhost"} or host.endswith((".local", ".internal", ".localhost")):
        raise UnsafeURL(f"local host '{host}' not allowed")
    if check_dns:
        try:
            ips = (resolver or default_resolver)(host)
        except OSError as e:
            raise UnsafeURL(f"cannot resolve {host}: {e}") from e
        bad = [ip for ip in ips if not _is_public_ip(ip)]
        if bad or not ips:
            raise UnsafeURL(f"{host} resolves to non-public address {bad or ips}")
    return url.strip()


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def hex_to_rgb(color: str, default: tuple[int, int, int] = (255, 255, 255)) -> tuple[int, int, int]:
    c = str(color).lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", c):
        return default
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
