"""API-key handling. Keys live ONLY in the .env file (git-ignored, chmod 600) or real environment variables.
They are never written to config/*.yaml, the database, logs or the dashboard."""
from __future__ import annotations

import os
import re
from pathlib import Path

from app.core.errors import ConfigurationError

KNOWN_KEYS = {
    "GROQ_API_KEY": "Groq cloud LLM (free tier available) - https://console.groq.com/keys",
    "LLM_API_KEY": "Key for the generic OpenAI-compatible provider (llamacpp entry)",
    "PEXELS_API_KEY": "Pexels free stock video/photos - https://www.pexels.com/api/",
    "PIXABAY_API_KEY": "Pixabay free stock media - https://pixabay.com/api/docs/",
    "DASHBOARD_TOKEN": "Password for the dashboard (needed only if not bound to localhost)",
    "NOTIFY_WEBHOOK_URL": "Optional Discord/Slack-style webhook for notifications",
}
_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


def key_status(cfg) -> list[dict]:
    """Which keys are set (never the values)."""
    return [{"name": k, "set": bool(cfg.env(k)), "purpose": v} for k, v in KNOWN_KEYS.items()]


def set_env_key(root: Path, name: str, value: str) -> Path:
    name = name.strip().upper()
    if not _NAME.match(name):
        raise ConfigurationError("key name must look like MY_API_KEY (capital letters, digits, underscores)")
    value = value.strip()
    if not value or any(c in value for c in "\r\n\x00"):
        raise ConfigurationError("value is empty or contains a line break")
    env = Path(root) / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.is_file() else []
    out, done = [], False
    for ln in lines:
        if ln.strip().startswith(f"{name}=") or ln.strip().startswith(f"# {name}="):
            if not done:
                out.append(f"{name}={value}")
                done = True
            continue
        out.append(ln)
    if not done:
        out.append(f"{name}={value}")
    tmp = env.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    os.replace(tmp, env)
    try:
        os.chmod(env, 0o600)
    except OSError:
        pass
    os.environ[name] = value
    return env
