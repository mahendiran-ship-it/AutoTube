"""YouTube OAuth 2.0 (installed-app flow) with locally stored, permission-restricted tokens.

Setup: create an OAuth *Desktop app* client in Google Cloud, enable "YouTube Data API v3" and
"YouTube Analytics API", download the client JSON and point YOUTUBE_CLIENT_SECRETS at it. See README.
Tokens are stored in data/secrets/ (git-ignored, chmod 600 where supported) and never logged.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from app.core.errors import ConfigurationError, NonRetryableError
from app.core.logger import get_logger

log = get_logger("youtube.auth")

SCOPE_UPLOAD = "https://www.googleapis.com/auth/youtube.upload"
SCOPE_READ = "https://www.googleapis.com/auth/youtube.readonly"
SCOPE_ANALYTICS = "https://www.googleapis.com/auth/yt-analytics.readonly"
SCOPE_MONETARY = "https://www.googleapis.com/auth/yt-analytics-monetary.readonly"


class AuthRequired(NonRetryableError):
    """The user must connect (or reconnect) their YouTube account."""


def _google():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        return Request, Credentials, InstalledAppFlow, build
    except ImportError as e:
        raise ConfigurationError("Google API libraries missing. Run: pip install google-api-python-client "
                                 "google-auth-oauthlib google-auth-httplib2") from e


class YouTubeAuth:
    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self.flow_state = {"running": False, "error": None}

    # ---- paths -----------------------------------------------------------
    @property
    def scopes(self) -> list[str]:
        s = [SCOPE_UPLOAD, SCOPE_READ, SCOPE_ANALYTICS]
        if self.cfg.get("youtube.request_monetary_scope", False):
            s.append(SCOPE_MONETARY)
        return s

    def client_secrets_path(self) -> Path | None:
        raw = self.cfg.env(self.cfg.get("youtube.client_secrets_env", "YOUTUBE_CLIENT_SECRETS"))
        if not raw:
            return None
        p = Path(raw)
        return p if p.is_absolute() else self.cfg.root / p

    @property
    def token_path(self) -> Path:
        return self.cfg.path("secrets_dir") / self.cfg.get("youtube.token_file", "youtube_token.json")

    # ---- state -------------------------------------------------------------
    def status(self) -> dict:
        secrets = self.client_secrets_path()
        tok = self.token_path
        info = {"client_secrets_configured": bool(secrets and secrets.is_file()), "client_secrets_path": str(secrets) if secrets else None,
                "token_present": tok.is_file(), "authorized": False, "scopes_granted": [], "needs_reauth_for": [],
                "flow_running": self.flow_state["running"], "flow_error": self.flow_state["error"]}
        if tok.is_file():
            try:
                data = json.loads(tok.read_text(encoding="utf-8"))
                granted = data.get("scopes") or []
                info["scopes_granted"] = granted
                info["authorized"] = bool(data.get("refresh_token") or data.get("token"))
                info["needs_reauth_for"] = [s for s in self.scopes if s not in granted]
            except (ValueError, OSError):
                info["authorized"] = False
        return info

    def _save(self, creds) -> None:
        tok = self.token_path
        tok.parent.mkdir(parents=True, exist_ok=True)
        tmp = tok.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(creds.to_json())
        os.replace(tmp, tok)
        try:
            os.chmod(tok, 0o600)
        except OSError:
            pass

    # ---- flows -------------------------------------------------------------
    def authorize(self, open_browser: bool = True, port: int = 0):
        """Interactive consent in the user's browser (runs a temporary localhost callback server)."""
        Request, Credentials, InstalledAppFlow, _ = _google()
        secrets = self.client_secrets_path()
        if not secrets or not secrets.is_file():
            raise ConfigurationError(
                "OAuth client file not found. Download the 'Desktop app' OAuth client JSON from Google Cloud and set "
                "YOUTUBE_CLIENT_SECRETS in .env (see README → YouTube API setup).")
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets), self.scopes)
        creds = flow.run_local_server(port=port, open_browser=open_browser, prompt="consent",
                                      authorization_prompt_message="Open this URL to connect your YouTube account:\n{url}",
                                      success_message="AutoTube AI is connected. You can close this tab.")
        self._save(creds)
        log.info("YouTube account connected")
        return creds

    def authorize_in_background(self) -> bool:
        """Used by the dashboard button. Returns False if a flow is already running."""
        with self._lock:
            if self.flow_state["running"]:
                return False
            self.flow_state.update(running=True, error=None)

        def work():
            try:
                self.authorize(open_browser=True)
            except Exception as e:
                self.flow_state["error"] = str(e)[:300]
            finally:
                self.flow_state["running"] = False
        threading.Thread(target=work, daemon=True, name="youtube-oauth").start()
        return True

    def credentials(self):
        Request, Credentials, _, _ = _google()
        tok = self.token_path
        if not tok.is_file():
            raise AuthRequired("YouTube is not connected. Run `python main.py youtube-auth` or use Dashboard → Settings → Connect YouTube.")
        try:
            creds = Credentials.from_authorized_user_file(str(tok), self.scopes)
        except Exception as e:
            raise AuthRequired(f"Stored YouTube token is unreadable ({type(e).__name__}); reconnect your account.") from e
        if not creds.valid:
            if creds.refresh_token:
                try:
                    creds.refresh(Request())
                    self._save(creds)
                except Exception as e:
                    raise AuthRequired("YouTube token expired or was revoked; reconnect your account "
                                       f"({type(e).__name__}). Note: apps in 'Testing' mode expire tokens after 7 days.") from e
            else:
                raise AuthRequired("YouTube token has no refresh token; reconnect your account.")
        return creds

    def service(self, api: str = "youtube", version: str = "v3"):
        _, _, _, build = _google()
        return build(api, version, credentials=self.credentials(), cache_discovery=False)

    def analytics_service(self):
        return self.service("youtubeAnalytics", "v2")

    def disconnect(self) -> bool:
        tok = self.token_path
        if tok.is_file():
            tok.unlink()
            return True
        return False
