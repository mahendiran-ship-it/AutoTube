"""Thumbnail engine: best-frame selection from the scene visuals (never from burned-in captions),
short readable phrase, strong contrast, consistent branding. Output JPEG < 2 MB (YouTube limit)."""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from app.core.logger import get_logger
from app.utils import ffmpeg as ff
from app.utils.fonts import load_font
from app.utils.validators import hex_to_rgb

log = get_logger("thumbnail")


def frame_score(img: Image.Image) -> float:
    """Higher = more visually engaging: contrast + colourfulness + mid exposure; blank/dark frames score ~0."""
    small = img.convert("RGB").resize((96, 170))
    a = np.asarray(small, np.float32)
    lum = a.mean(axis=2)
    mean, std = float(lum.mean()), float(lum.std())
    if mean < 14 or std < 6:
        return 0.0
    rg, yb = a[..., 0] - a[..., 1], 0.5 * (a[..., 0] + a[..., 1]) - a[..., 2]
    colorful = float(np.sqrt(rg.std() ** 2 + yb.std() ** 2) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2))
    hist = np.histogram(lum, bins=32, range=(0, 255))[0].astype(np.float32)
    p = hist / hist.sum()
    entropy = float(-(p[p > 0] * np.log2(p[p > 0])).sum())
    exposure = 1.0 - abs(mean - 85) / 140
    # bright centre (subject) bonus
    h, w = lum.shape
    centre = float(lum[h // 4: 3 * h // 4, w // 4: 3 * w // 4].mean())
    return round(std * 0.6 + colorful * 0.5 + entropy * 4 + max(0.0, exposure) * 20 + min(centre, 150) * 0.08, 2)


def best_frame(sources: list[Path | str], samples_per_clip: int = 3) -> tuple[Image.Image, dict]:
    best, best_s, meta = None, -1.0, {}
    with tempfile.TemporaryDirectory() as d:
        for src in sources:
            try:
                dur = ff.duration_of(src)
            except Exception:
                continue
            for k in range(samples_per_clip):
                t = dur * (0.25 + 0.5 * k / max(1, samples_per_clip - 1))
                try:
                    p = ff.extract_frame(src, t, Path(d) / "f.jpg")
                    img = Image.open(p).convert("RGB")
                    img.load()
                except Exception:
                    continue
                s = frame_score(img)
                if s > best_s:
                    best, best_s, meta = img.copy(), s, {"source": str(src), "t": round(t, 2), "score": s}
    if best is None:
        raise RuntimeError("no usable frame found for thumbnail")
    return best, meta


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    lines, cur = [], ""
    for w in text.split():
        trial = f"{cur} {w}".strip()
        if cur and draw.textlength(trial, font=font) > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


class ThumbnailGenerator:
    def __init__(self, cfg):
        self.cfg = cfg

    def sanitize_text(self, text: str) -> str:
        max_words = int(self.cfg.get("thumbnail.max_words", 5))
        text = re.sub(r"<[^>]*>", " ", text or "")
        text = re.sub(r"[^\w\s?!.'’-]", "", text).strip()
        for bad in self.cfg.get("seo.banned_title_phrases") or []:
            text = re.sub(re.escape(bad), "", text, flags=re.I)
        return " ".join(text.split()[:max_words]).upper()

    def create(self, sources: list[Path | str], text: str, out_path: Path | str, long_form: bool = False) -> dict:
        size = tuple(self.cfg.get("thumbnail.long_size" if long_form else "thumbnail.shorts_size", [1080, 1920]))
        W, H = int(size[0]), int(size[1])
        frame, meta = best_frame(sources)
        img = ImageOps.fit(frame, (W, H), Image.LANCZOS, centering=(0.5, 0.45))
        img = ImageEnhance.Contrast(img).enhance(1.12)
        img = ImageEnhance.Color(img).enhance(1.15)
        # darkening gradients (top for brand, lower-middle for headline) → guaranteed text contrast
        grad = Image.new("L", (1, H))
        for y in range(H):
            v = int(150 * max(0.0, (0.46 - y / H) / 0.46) ** 1.5 * 0.7 + 190 * max(0.0, (y / H - 0.50) / 0.5) ** 1.3)
            grad.putpixel((0, y), min(235, v))
        img = Image.composite(Image.new("RGB", (W, H), (3, 4, 12)), img, grad.resize((W, H)))
        accent = hex_to_rgb(self.cfg.get("channel.branding.accent_color", self.cfg.get("thumbnail.accent_color", "#FFB020")))
        d = ImageDraw.Draw(img)
        phrase = self.sanitize_text(text)
        if phrase:
            fs = int(W * (0.15 if not long_form else 0.1))
            font = load_font(self.cfg, fs, True)
            lines = _wrap(d, phrase, font, int(W * 0.86))
            while len(lines) > 3 and fs > 40:
                fs = int(fs * 0.88)
                font = load_font(self.cfg, fs, True)
                lines = _wrap(d, phrase, font, int(W * 0.86))
            lh = int(fs * 1.12)
            y0 = int(H * (0.60 if not long_form else 0.55))
            for i, line in enumerate(lines):
                w = d.textlength(line, font=font)
                color = accent if i == len(lines) - 1 else (255, 255, 255)
                d.text(((W - w) / 2, y0 + i * lh), line, font=font, fill=color, stroke_width=max(4, fs // 14), stroke_fill=(0, 0, 0))
            bar_w = int(W * 0.18)
            d.rectangle([(W - bar_w) / 2, y0 - int(fs * 0.25), (W + bar_w) / 2, y0 - int(fs * 0.25) + max(6, fs // 16)], fill=accent)
        brand = self.cfg.get("channel.branding.watermark_text") or self.cfg.get("channel.name", "")
        if brand:
            bf = load_font(self.cfg, int(W * 0.04), True)
            bw = d.textlength(brand, font=bf)
            d.text(((W - bw) / 2, int(H * 0.045)), brand, font=bf, fill=(235, 240, 255), stroke_width=2, stroke_fill=(0, 0, 0))
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        q = 92
        while True:
            img.save(out_path, "JPEG", quality=q, optimize=True)
            if out_path.stat().st_size <= 1_900_000 or q <= 60:
                break
            q -= 8
        return {"path": str(out_path), "size": [W, H], "phrase": phrase, "frame": meta,
                "bytes": out_path.stat().st_size}
