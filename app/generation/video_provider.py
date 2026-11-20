"""AI video generation adapter (Provider A).

HONESTY NOTE: there is no verified, free, unlimited hosted text-to-video API this project can call, so none is
hard-coded. Instead this is a *command adapter*: you point it at any local script/CLI (ComfyUI wrapper,
Wan/LTX/CogVideoX script, a paid provider's SDK script...) and it runs it WITHOUT a shell:

    visuals.video_ai.command: 'python my_gen.py --prompt {prompt} --seconds {duration} --out {output}'

Placeholders: {prompt} {duration} {width} {height} {output}. The result is probed; garbage output is rejected.
Mark `paid: true` for paid tools: the budget controller then blocks them unless you explicitly allowed a budget.
"""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from app.core.errors import ConfigurationError, ProviderUnavailable
from app.utils import ffmpeg as ff


class CommandVideoProvider:
    name = "video_ai_command"

    def __init__(self, cfg):
        c = cfg.get("visuals.video_ai", {}) or {}
        self.enabled_flag = bool(c.get("enabled", False))
        self.template = str(c.get("command", "") or "")
        self.timeout = float(c.get("timeout_seconds", 900))
        self.is_paid = bool(c.get("paid", False))

    def enabled(self) -> bool:
        return self.enabled_flag and bool(self.template.strip())

    def health(self) -> tuple[bool, str]:
        if not self.template.strip():
            return False, "visuals.video_ai.command is empty"
        exe = shlex.split(self.template)[0]
        return True, f"configured ({exe})"

    def generate(self, prompt: str, out_path: Path, duration: float, width: int, height: int) -> Path:
        if not self.enabled():
            raise ConfigurationError("video_ai provider is not enabled/configured")
        subs = {"{prompt}": prompt[:1500], "{duration}": f"{duration:.2f}", "{width}": str(width),
                "{height}": str(height), "{output}": str(out_path)}
        argv = []
        for tok in shlex.split(self.template):
            for k, v in subs.items():
                tok = tok.replace(k, v)
            argv.append(tok)
        try:
            cp = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ProviderUnavailable(f"video_ai command failed to run: {e}") from e
        if cp.returncode != 0:
            raise ProviderUnavailable(f"video_ai command exited {cp.returncode}: {(cp.stderr or '')[-300:]}")
        out_path = Path(out_path)
        if not out_path.is_file():
            raise ProviderUnavailable("video_ai command did not create the output file")
        info = ff.probe(out_path)
        if not info["has_video"] or info["duration"] < 1.0:
            raise ProviderUnavailable("video_ai output is not a valid video")
        return out_path
