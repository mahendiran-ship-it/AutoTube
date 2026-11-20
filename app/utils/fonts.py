"""Locate a usable TrueType font on Windows / macOS / Linux (or use a configured/bundled one)."""
from __future__ import annotations

import os
from pathlib import Path

from PIL import ImageFont

_BOLD = ["arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "Helvetica-Bold.ttf",
         "NotoSans-Bold.ttf", "Roboto-Bold.ttf", "Montserrat-Bold.ttf", "Impact.ttf"]
_REG = ["arial.ttf", "Arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf", "Helvetica.ttc", "NotoSans-Regular.ttf",
        "Roboto-Regular.ttf"]


def _dirs(extra: Path | None) -> list[Path]:
    dirs = [extra] if extra else []
    dirs += [Path(p) for p in (
        os.environ.get("WINDIR", "C:/Windows") + "/Fonts", "/usr/share/fonts", "/usr/local/share/fonts",
        str(Path.home() / ".fonts"), str(Path.home() / ".local/share/fonts"), "/Library/Fonts", "/System/Library/Fonts",
        str(Path.home() / "Library/Fonts"))]
    return [d for d in dirs if d and d.exists()]


def find_font_path(cfg=None, bold: bool = True) -> str | None:
    if cfg is not None:
        configured = cfg.get("thumbnail.font_path") or ""
        if configured and Path(configured).is_file():
            return configured
        assets = cfg.path("assets_dir", "fonts", create=False)
    else:
        assets = None
    wanted = _BOLD + _REG if bold else _REG + _BOLD
    for d in _dirs(assets):
        for name in wanted:
            for hit in d.rglob(name):
                return str(hit)
    return None


def load_font(cfg=None, size: int = 48, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    p = find_font_path(cfg, bold)
    if p:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    try:                                   # Pillow >= 10.1 ships a scalable default font
        return ImageFont.load_default(size=size)
    except TypeError:                      # older Pillow: fixed-size bitmap font
        return ImageFont.load_default()
