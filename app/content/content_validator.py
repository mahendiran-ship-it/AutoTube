"""Script validation (structure, length, banned phrasing) and duration estimation."""
from __future__ import annotations

import re

from app.utils.text import split_sentences, word_count

SECTIONS = ["hook", "question", "story", "escalation", "payoff", "ending"]
_CTA = re.compile(r"\b(subscribe|like and|hit the bell|comment below|follow (us|for)|link in (the )?description)\b", re.I)


def estimate_duration(text: str, wpm: float = 150, sentence_pause: float = 0.22, speed: float = 1.0) -> float:
    words = word_count(text)
    sentences = max(1, len(split_sentences(text)))
    return round(words / (wpm * max(speed, 0.3)) * 60 + sentences * sentence_pause, 2)


class ContentValidator:
    def __init__(self, cfg):
        self.cfg = cfg

    def target(self) -> dict:
        fmt = self.cfg.get("channel.format.short_seconds", {}) or {}
        return {"min": float(fmt.get("min", 30)), "max": float(fmt.get("max", 58)), "target": float(fmt.get("target", 45))}

    def target_words(self) -> tuple[int, int]:
        wpm = float(self.cfg.get("content.words_per_minute", 150))
        t = self.target()
        return int(t["target"] / 60 * wpm * 0.9), int(t["max"] / 60 * wpm * 0.92)

    def validate(self, sections: list[dict]) -> dict:
        issues: list[dict] = []
        names = [s.get("section") for s in sections]
        if names != SECTIONS:
            issues.append({"severity": "error", "type": "structure",
                           "detail": f"sections must be exactly {SECTIONS}, got {names}"})
        narration = " ".join(str(s.get("text", "")).strip() for s in sections).strip()
        if not narration:
            return {"passed": False, "issues": issues + [{"severity": "error", "type": "empty", "detail": "empty narration"}],
                    "words": 0, "duration": 0.0}
        max_hook = int(self.cfg.get("content.max_hook_words", 18))
        hook = next((s["text"] for s in sections if s.get("section") == "hook"), "")
        if word_count(hook) > max_hook:
            issues.append({"severity": "error", "type": "hook_too_long",
                           "detail": f"hook has {word_count(hook)} words (max {max_hook})"})
        lowered = narration.lower()
        for ph in self.cfg.get("content.banned_phrases") or []:
            if ph.lower() in lowered:
                issues.append({"severity": "error", "type": "banned_phrase", "detail": f"contains '{ph}'"})
        if _CTA.search(narration):
            issues.append({"severity": "error", "type": "call_to_action", "detail": "contains a subscribe/like call to action"})
        if re.match(r"\s*(hello|hi|hey|welcome|greetings)\b", narration, re.I):
            issues.append({"severity": "error", "type": "generic_intro", "detail": "starts with a generic greeting"})
        max_sent = int(self.cfg.get("content.max_sentence_words", 26))
        for s in split_sentences(narration):
            if word_count(s) > max_sent:
                issues.append({"severity": "warning", "type": "long_sentence",
                               "detail": f"{word_count(s)} words: '{s[:60]}…'"})
        wpm = float(self.cfg.get("content.words_per_minute", 150))
        dur = estimate_duration(narration, wpm, float(self.cfg.get("video.sentence_pause_seconds", 0.22)),
                                float(self.cfg.get("voice.speed", 1.0)))
        tgt, tol = self.target(), float(self.cfg.get("content.duration_tolerance", 0.18))
        if dur < tgt["min"] * (1 - tol):
            issues.append({"severity": "error", "type": "too_short", "detail": f"≈{dur}s, minimum {tgt['min']}s"})
        if dur > tgt["max"] * (1 + tol / 2):
            issues.append({"severity": "error", "type": "too_long", "detail": f"≈{dur}s, maximum {tgt['max']}s"})
        if re.search(r"[\U0001F300-\U0001FAFF]", narration):
            issues.append({"severity": "warning", "type": "emoji", "detail": "emoji in narration"})
        return {"passed": not any(i["severity"] == "error" for i in issues), "issues": issues,
                "words": word_count(narration), "duration": dur}
