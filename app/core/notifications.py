"""Notifications: terminal + database always; optional webhook adapter. Adapters never raise."""
from __future__ import annotations

import json
from abc import ABC, abstractmethod

import requests

from app.core.logger import get_logger, redact

log = get_logger("notify")

# Event names
VIDEO_COMPLETED = "video_completed"
VIDEO_FAILED = "video_failed"
NEEDS_APPROVAL = "needs_approval"
VIDEO_UPLOADED = "video_uploaded"
VIDEO_PUBLISHED = "video_published"
AUTH_REQUIRED = "auth_required"
DISK_LOW = "disk_low"
PROVIDER_FAILED = "provider_failed"
REPORT_READY = "report_ready"
INFO = "info"

_ICON = {"info": "i", "warning": "!", "error": "x", "success": "+"}


class NotificationAdapter(ABC):
    name = "adapter"

    @abstractmethod
    def send(self, level: str, event: str, title: str, message: str) -> None: ...


class TerminalAdapter(NotificationAdapter):
    name = "terminal"

    def send(self, level, event, title, message):
        print(f"[{_ICON.get(level, 'i')}] {title}: {message}", flush=True)


class WebhookAdapter(NotificationAdapter):
    """POSTs {"content": "..."} (Discord) plus {"text": "..."} (Slack) — works for both."""
    name = "webhook"

    def __init__(self, url: str, timeout: float = 8):
        self.url, self.timeout = url, timeout

    def send(self, level, event, title, message):
        text = redact(f"**{title}** — {message}")
        requests.post(self.url, data=json.dumps({"content": text, "text": text}),
                      headers={"Content-Type": "application/json"}, timeout=self.timeout)


class Notifier:
    def __init__(self, repo, cfg=None, adapters: list[NotificationAdapter] | None = None):
        self.repo = repo
        self.adapters: list[NotificationAdapter] = adapters if adapters is not None else []
        if adapters is None and cfg is not None:
            if cfg.get("notifications.terminal", True):
                self.adapters.append(TerminalAdapter())
            if cfg.get("notifications.webhook.enabled", False):
                url = cfg.env(cfg.get("notifications.webhook.url_env", "NOTIFY_WEBHOOK_URL"))
                if url:
                    self.adapters.append(WebhookAdapter(url))
                else:
                    log.warning("webhook notifications enabled but URL env var is empty")

    def notify(self, event: str, title: str, message: str = "", level: str = "info",
               video_id: int | None = None) -> None:
        message = redact(message)
        try:
            self.repo.add_notification(level, event, title, message, video_id)
        except Exception as e:  # pragma: no cover - DB failure must not break pipeline
            log.error("could not store notification: %s", e)
        for a in self.adapters:
            try:
                a.send(level, event, title, message)
            except Exception as e:
                log.warning("notification adapter %s failed: %s", a.name, e)
