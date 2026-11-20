"""Structured logging with secret redaction and an optional SQLite sink."""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import sys
import threading
from datetime import datetime, timezone

_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"ya29\.[0-9A-Za-z_\-]+"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|access_token|refresh_token|client_secret|token|secret|password|authorization)(\"?\s*[:=]\s*\"?)([^\s\"',}&]+)"),
]
_SECRET_ENV_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)", re.I)


def redact(text: str) -> str:
    if not text:
        return text
    out = str(text)
    for name, val in os.environ.items():
        if val and len(val) >= 8 and _SECRET_ENV_RE.search(name) and val in out:
            out = out.replace(val, "***")
    out = _PATTERNS[0].sub("***", out)
    out = _PATTERNS[1].sub("***", out)
    out = _PATTERNS[2].sub("Bearer ***", out)
    out = _PATTERNS[3].sub(lambda m: f"{m.group(1)}{m.group(2)}***", out)
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for k in ("task_id", "category", "video_id", "stage"):
            if hasattr(record, k):
                payload[k] = getattr(record, k)
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


class RedactingConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class DBLogHandler(logging.Handler):
    """Writes WARNING+ (and tagged INFO) records to the `logs` table. Never raises."""

    def __init__(self, repo, level=logging.INFO):
        super().__init__(level)
        self.repo = repo
        self._busy = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._busy, "on", False):
            return
        self._busy.on = True
        try:
            extra = {}
            if record.exc_info:
                extra["exc"] = redact(self.format_exc(record))
            self.repo.add_log(
                level=record.levelname, logger=record.name,
                message=redact(record.getMessage()),
                task_id=getattr(record, "task_id", None),
                category=getattr(record, "category", None) or record.name.split(".")[-1],
                extra=extra or None,
            )
        except Exception:
            pass
        finally:
            self._busy.on = False

    @staticmethod
    def format_exc(record):
        return logging.Formatter().formatException(record.exc_info)


_configured = False


def setup_logging(cfg=None, force: bool = False) -> logging.Logger:
    global _configured
    root = logging.getLogger("autotube")
    if _configured and not force:
        return root
    for h in list(root.handlers):
        root.removeHandler(h)
    level = getattr(logging, str(cfg.get("logging.level", "INFO") if cfg else "INFO").upper(), logging.INFO)
    root.setLevel(level)
    root.propagate = False
    if cfg is None or cfg.get("logging.console", True):
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(RedactingConsoleFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
        root.addHandler(ch)
    if cfg is not None and cfg.get("logging.json_file", True):
        logs_dir = cfg.path("logs_dir")
        fh = logging.handlers.RotatingFileHandler(
            logs_dir / "autotube.jsonl", maxBytes=int(cfg.get("logging.max_bytes", 5242880)),
            backupCount=int(cfg.get("logging.backups", 5)), encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)
    _configured = True
    return root


def attach_db(repo) -> DBLogHandler:
    root = logging.getLogger("autotube")
    for h in root.handlers:
        if isinstance(h, DBLogHandler):
            root.removeHandler(h)
    handler = DBLogHandler(repo)
    root.addHandler(handler)
    return handler


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"autotube.{name}")
