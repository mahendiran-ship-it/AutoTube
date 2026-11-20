"""Optional local AI image generation via AUTOMATIC1111 / Forge (`--api`). Disabled by default.

API used: GET /sdapi/v1/sd-models (health), POST /sdapi/v1/txt2img → {"images": [base64 PNG, ...]}.
Image generation is slow on CPU; the manager only calls it when enabled and the machine passes the resource check.
"""
from __future__ import annotations

import base64
from pathlib import Path

import requests

from app.core.errors import ProviderUnavailable

NEGATIVE = "text, watermark, logo, people, faces, hands, blurry, low quality, deformed, cartoon"


class AutomaticImageProvider:
    name = "automatic1111"
    is_paid = False

    def __init__(self, cfg):
        self.cfg = cfg
        c = cfg.get("visuals.image_ai", {}) or {}
        self.enabled_flag = bool(c.get("enabled", False))
        self.url = (cfg.env(c.get("url_env", "SD_WEBUI_URL")) or c.get("default_url", "http://127.0.0.1:7860")).rstrip("/")
        self.steps = int(c.get("steps", 22))
        self.timeout = float(c.get("timeout_seconds", 300))

    def enabled(self) -> bool:
        return self.enabled_flag

    def health(self) -> tuple[bool, str]:
        try:
            r = requests.get(f"{self.url}/sdapi/v1/sd-models", timeout=4)
            return (r.status_code == 200, "ok" if r.status_code == 200 else f"HTTP {r.status_code}")
        except Exception as e:
            return False, f"not reachable at {self.url} ({type(e).__name__}); start the WebUI with --api"

    def generate(self, prompt: str, out_path: Path, width: int = 512, height: int = 896, seed: int = -1) -> Path:
        body = {"prompt": prompt, "negative_prompt": NEGATIVE, "steps": self.steps, "width": width - width % 8,
                "height": height - height % 8, "cfg_scale": 7, "seed": seed}
        try:
            r = requests.post(f"{self.url}/sdapi/v1/txt2img", json=body, timeout=self.timeout)
            r.raise_for_status()
            img = r.json()["images"][0]
        except (requests.RequestException, KeyError, IndexError, ValueError) as e:
            raise ProviderUnavailable(f"image generation failed: {type(e).__name__}: {e}") from e
        out_path = Path(out_path)
        out_path.write_bytes(base64.b64decode(img.split(",", 1)[-1]))
        return out_path
