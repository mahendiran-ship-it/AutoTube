"""Configuration loading.

Layers (later wins):  config/settings.yaml  <  channel.yaml (under `channel`)
<  sources.yaml (under `sources`)  <  config/local.yaml (dashboard overrides).
Secrets are only ever read from environment variables / .env.
"""
from __future__ import annotations

import copy
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from app.core.errors import ConfigurationError

PROJECT_ROOT = Path(os.environ.get("AUTOTUBE_HOME") or Path(__file__).resolve().parents[2]).resolve()

_SECRET_KEY_RE = re.compile(r"(api[_-]?key|token|secret|password|client_secret)", re.I)
_HHMM = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_dotenv(path: Path) -> int:
    """Minimal .env loader. Never overrides variables already in the environment."""
    if not path.is_file():
        return 0
    n = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key and key not in os.environ and val != "":
            os.environ[key] = val
            n += 1
    return n


def _read_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigurationError(f"Invalid YAML in {path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigurationError(f"{path} must contain a YAML mapping")
    return data


class Config:
    def __init__(self, data: dict, root: Path | None = None, config_dir: Path | None = None):
        self.data = data
        self.root = Path(root or PROJECT_ROOT)
        self.config_dir = Path(config_dir or self.root / "config")

    # ---- construction -------------------------------------------------
    @classmethod
    def load(cls, config_dir: Path | str | None = None, root: Path | str | None = None,
             overrides: dict | None = None, load_env: bool = True) -> "Config":
        root_p = Path(root or PROJECT_ROOT)
        cdir = Path(config_dir or root_p / "config")
        if load_env:
            load_dotenv(root_p / ".env")
        data = _read_yaml(cdir / "settings.yaml")
        data["channel"] = deep_merge(data.get("channel", {}), _read_yaml(cdir / "channel.yaml"))
        data["sources"] = deep_merge(data.get("sources", {}), _read_yaml(cdir / "sources.yaml"))
        data = deep_merge(data, _read_yaml(cdir / "local.yaml"))
        if overrides:
            data = deep_merge(data, overrides)
        cfg = cls(data, root_p, cdir)
        return cfg

    # ---- access -------------------------------------------------------
    def get(self, path: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur

    def require(self, path: str) -> Any:
        val = self.get(path)
        if val is None:
            raise ConfigurationError(f"Missing required setting: {path}")
        return val

    def set(self, path: str, value: Any) -> None:
        parts = path.split(".")
        cur = self.data
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value

    def env(self, name: str | None, default: str | None = None) -> str | None:
        if not name:
            return default
        val = os.environ.get(name)
        return val if val else default

    # ---- paths --------------------------------------------------------
    def path(self, key: str, *parts: str, create: bool = True) -> Path:
        """Resolve a configured directory (relative to project root)."""
        base = Path(self.get(f"paths.{key}", key))
        if not base.is_absolute():
            base = self.root / base
        p = base.joinpath(*parts) if parts else base
        if create:
            p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        p = Path(self.get("database.path", "data/autotube.db"))
        p = p if p.is_absolute() else self.root / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def tz(self) -> ZoneInfo:
        name = self.get("channel.timezone") or self.get("app.timezone") or "UTC"
        try:
            return ZoneInfo(name)
        except Exception as e:  # tzdata missing on Windows, bad name...
            raise ConfigurationError(
                f"Unknown timezone '{name}'. On Windows run: pip install tzdata ({e})") from e

    @property
    def channel(self) -> dict:
        return self.get("channel", {})

    @property
    def publishing_mode(self) -> str:
        return str(self.get("pipeline.publishing_mode", "REVIEW")).upper()

    # ---- validation ---------------------------------------------------
    def validate(self) -> list[str]:
        errs: list[str] = []
        if self.publishing_mode not in {"MANUAL", "REVIEW", "PRIVATE", "AUTO"}:
            errs.append("pipeline.publishing_mode must be MANUAL, REVIEW, PRIVATE or AUTO")
        for k in ("schedule.production_time", "schedule.publish_time", "schedule.analytics_time"):
            if not _HHMM.match(str(self.get(k, ""))):
                errs.append(f"{k} must look like HH:MM")
        try:
            _ = self.tz
        except ConfigurationError as e:
            errs.append(str(e))
        smin = self.get("channel.format.short_seconds.min")
        smax = self.get("channel.format.short_seconds.max")
        if not (isinstance(smin, (int, float)) and isinstance(smax, (int, float)) and 0 < smin < smax <= 180):
            errs.append("channel.format.short_seconds min/max invalid (0 < min < max <= 180)")
        if not self.get("channel.name"):
            errs.append("channel.name is required")
        preset = self.get("video.preset", "auto")
        if preset != "auto" and preset not in (self.get("video.presets") or {}):
            errs.append(f"video.preset '{preset}' is not defined in video.presets")
        if float(self.get("budget.monthly_limit_inr", 0) or 0) < 0:
            errs.append("budget.monthly_limit_inr cannot be negative")
        if not self.get("ai.fallback_order"):
            errs.append("ai.fallback_order must list at least one provider")
        return errs

    # ---- persistence of dashboard overrides ---------------------------
    def save_local_override(self, updates: dict) -> None:
        """Deep-merge `updates` into config/local.yaml (atomic). Rejects secret-looking keys."""
        def check(d: dict, trail: str = ""):
            for k, v in d.items():
                if _SECRET_KEY_RE.search(str(k)) and not str(k).endswith("_env"):
                    raise ConfigurationError(f"Refusing to store secret-like key '{trail}{k}' in config. Use .env.")
                if isinstance(v, dict):
                    check(v, f"{trail}{k}.")
        check(updates)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        target = self.config_dir / "local.yaml"
        current = _read_yaml(target)
        merged = deep_merge(current, updates)
        fd, tmp = tempfile.mkstemp(dir=self.config_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                yaml.safe_dump(merged, fh, sort_keys=False, allow_unicode=True)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        self.data = deep_merge(self.data, updates)

    def public_dict(self) -> dict:
        """Config safe to show in the dashboard (no secret values; env names only)."""
        def scrub(d):
            if isinstance(d, dict):
                return {k: ("***" if (_SECRET_KEY_RE.search(k) and not k.endswith("_env") and isinstance(v, str)) else scrub(v))
                        for k, v in d.items()}
            if isinstance(d, list):
                return [scrub(x) for x in d]
            return d
        return scrub(copy.deepcopy(self.data))
