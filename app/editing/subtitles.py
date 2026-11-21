"""Subtitles: SRT (separate file) + ASS (burn-in styling, safe zones).

Timing honesty: sentence start/end come from the actual synthesized audio (exact). Inside a sentence,
words are timed proportionally to their character length (an estimate). No forced aligner is required.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.utils.validators import hex_to_rgb


@dataclass
class Caption:
    start: float
    end: float
    text: str


def _chunk_words(words: list[str], max_words: int, max_chars: int) -> list[list[str]]:
    """Greedy chunks of <= max_words words and <= ~2 lines of max_chars."""
    chunks, cur = [], []
    for w in words:
        trial = cur + [w]
        if cur and (len(trial) > max_words or len(" ".join(trial)) > max_chars * 2):
            chunks.append(cur)
            cur = [w]
        else:
            cur = trial
        if cur and re.search(r"[.!?]$", cur[-1]):      # break after sentence-ending punctuation
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    # avoid a lone 1-word orphan chunk when it can join the previous one
    merged: list[list[str]] = []
    for c in chunks:
        if merged and len(c) == 1 and len(merged[-1]) < max_words and not re.search(r"[.!?]$", merged[-1][-1]):
            merged[-1].extend(c)
        else:
            merged.append(c)
    return merged


def wrap_lines(text: str, max_chars: int) -> str:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return "\n".join(lines)


def build_captions(scene_timings: list[dict], cfg) -> list[Caption]:
    max_words = int(cfg.get("subtitles.max_words_per_caption", 4))
    max_chars = int(cfg.get("subtitles.max_chars_per_line", 22))
    min_len = float(cfg.get("subtitles.min_caption_seconds", 0.45))
    upper = bool(cfg.get("subtitles.uppercase", True))
    caps: list[Caption] = []
    for sc in scene_timings:
        for sent in sc["sentences"]:
            words = sent["text"].split()
            if not words:
                continue
            chunks = _chunk_words(words, max_words, max_chars)
            weights = [sum(len(w) + 1 for w in c) for c in chunks]
            total_w = sum(weights)
            dur = sent["end"] - sent["start"]
            t = sent["start"]
            for c, wt in zip(chunks, weights):
                d = dur * wt / total_w
                text = " ".join(c)
                caps.append(Caption(round(t, 3), round(t + d, 3), (text.upper() if upper else text)))
                t += d
    # enforce minimum on-screen time without overlapping the next caption
    for i, c in enumerate(caps):
        nxt = caps[i + 1].start if i + 1 < len(caps) else c.end + 1
        if c.end - c.start < min_len:
            c.end = round(min(c.start + min_len, nxt), 3)
        if i + 1 < len(caps) and c.end > caps[i + 1].start:
            c.end = caps[i + 1].start
    return [c for c in caps if c.end > c.start]


def _srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(captions: list[Caption], path: Path, cfg=None) -> Path:
    max_chars = int(cfg.get("subtitles.max_chars_per_line", 22)) if cfg else 22
    lines = []
    for i, c in enumerate(captions, 1):
        lines += [str(i), f"{_srt_time(c.start)} --> {_srt_time(c.end)}", wrap_lines(c.text, max_chars), ""]
    Path(path).write_text("\n".join(lines), encoding="utf-8")
    return Path(path)


def _ass_time(t: float) -> str:
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_color(hex_color: str, alpha: int = 0) -> str:
    r, g, b = hex_to_rgb(hex_color)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def default_font_name(cfg=None) -> str:
    f = (cfg.get("subtitles.font", "auto") if cfg else "auto")
    if f and f != "auto":
        return f
    import os
    import platform
    if os.name == "nt":
        return "Arial"
    return "Helvetica" if platform.system() == "Darwin" else "DejaVu Sans"


def write_ass(captions: list[Caption], path: Path, width: int, height: int, cfg) -> Path:
    size = int(round(float(cfg.get("subtitles.font_size_ratio", 0.052)) * height))
    outline = max(2, round(float(cfg.get("subtitles.outline_ratio", 0.0035)) * height))
    shadow = max(1, round(float(cfg.get("subtitles.shadow_ratio", 0.0015)) * height))
    margin_v = int(round(height * float(cfg.get("subtitles.bottom_margin_percent", 32)) / 100))
    margin_lr = int(round(width * float(cfg.get("subtitles.side_margin_percent", 9)) / 100))
    max_chars = int(cfg.get("subtitles.max_chars_per_line", 22))
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Main,{default_font_name(cfg)},{size},{_ass_color(cfg.get('subtitles.primary_color', '#FFFFFF'))},"
        f"&H000000FF,{_ass_color(cfg.get('subtitles.outline_color', '#000000'))},&H96000000,-1,0,0,0,100,100,1,0,1,"
        f"{outline},{shadow},2,{margin_lr},{margin_lr},{margin_v},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    body = []
    for c in captions:
        text = wrap_lines(c.text, max_chars).replace("\n", "\\N").replace("{", "(").replace("}", ")")
        body.append(f"Dialogue: 0,{_ass_time(c.start)},{_ass_time(c.end)},Main,,0,0,0,,{{\\fad(60,60)}}{text}")
    Path(path).write_text(head + "\n".join(body) + "\n", encoding="utf-8")
    return Path(path)


def validate_captions(captions: list[Caption], total_duration: float, cfg, narration: str = "") -> dict:
    problems = []
    max_words = int(cfg.get("subtitles.max_words_per_caption", 4))
    if not captions:
        return {"ok": False, "problems": ["no captions"], "count": 0}
    for i, c in enumerate(captions):
        if len(c.text.split()) > max_words + 1:
            problems.append(f"caption {i} has {len(c.text.split())} words")
        if c.end <= c.start:
            problems.append(f"caption {i} has non-positive duration")
        if i and c.start < captions[i - 1].end - 1e-3:
            problems.append(f"caption {i} overlaps the previous one")
        if c.end > total_duration + 0.05:
            problems.append(f"caption {i} ends after the video")
    if narration:
        spoken = len(re.findall(r"\w+", narration))
        shown = sum(len(re.findall(r"\w+", c.text)) for c in captions)
        if abs(spoken - shown) > max(2, 0.05 * spoken):
            problems.append(f"captions cover {shown} words but narration has {spoken}")
    return {"ok": not problems, "problems": problems, "count": len(captions),
            "last_end": captions[-1].end}
