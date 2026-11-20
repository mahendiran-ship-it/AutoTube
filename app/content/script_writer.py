"""Script writer. LLM path: generate -> validate -> fact-check -> feed errors back (bounded attempts).

Fallback path (no LLM): a plainly worded script assembled only from sentences in the research record.
It is flagged `generated_by="extractive"` and never auto-published (see pipeline).
"""
from __future__ import annotations

import re

from app.ai.base import render_prompt
from app.content.content_validator import SECTIONS, ContentValidator, estimate_duration
from app.core.errors import ValidationFailed
from app.core.logger import get_logger
from app.utils.text import split_sentences, truncate, word_count

log = get_logger("script")

SFX_CHOICES = ["none", "low_boom", "whoosh", "shimmer", "riser", "heartbeat"]
_DEFAULT_SFX = {"hook": "low_boom", "question": "none", "story": "shimmer", "escalation": "riser",
                "payoff": "low_boom", "ending": "shimmer"}


class ScriptWriter:
    def __init__(self, cfg, ai, validator: ContentValidator, fact_checker):
        self.cfg, self.ai, self.validator, self.fact_checker = cfg, ai, validator, fact_checker

    # ---- shared -------------------------------------------------------
    def _pronunciations(self, narration: str, extra: list | None = None) -> list[dict]:
        out = {}
        for word, say in (self.cfg.get("voice.pronunciations") or {}).items():
            if re.search(r"\b" + re.escape(word) + r"\b", narration):
                out[word] = say
        for p in extra or []:
            if isinstance(p, dict) and p.get("word") and p.get("say_as") and p["word"] in narration:
                out.setdefault(p["word"], p["say_as"])
        return [{"word": k, "say_as": v} for k, v in out.items()]

    def _captions(self, narration: str) -> list[str]:
        return split_sentences(narration)

    def _package(self, sections, generated_by, provider, validation, factcheck, notes="", extra_pron=None) -> dict:
        narration = " ".join(s["text"].strip() for s in sections)
        return {
            "narration": narration, "sections": sections, "generated_by": generated_by, "provider": provider,
            "estimated_duration": validation.get("duration") or estimate_duration(narration),
            "pronunciation": self._pronunciations(narration, extra_pron), "captions": self._captions(narration),
            "validation": validation, "factcheck": factcheck, "uncertainty_notes": notes,
            "sfx_suggestions": [s.get("sfx", "none") for s in sections],
            "visual_suggestions": [s.get("visual", "") for s in sections],
        }

    @staticmethod
    def _clean_sections(raw) -> list[dict]:
        out = []
        for s in raw or []:
            if not isinstance(s, dict):
                continue
            sec = str(s.get("section", "")).strip().lower()
            text = re.sub(r"\s+", " ", str(s.get("text", ""))).strip()
            sfx = s.get("sfx") if s.get("sfx") in SFX_CHOICES else _DEFAULT_SFX.get(sec, "none")
            out.append({"section": sec, "text": text, "visual": str(s.get("visual", ""))[:300], "sfx": sfx})
        by = {s["section"]: s for s in out}
        return [by[n] for n in SECTIONS if n in by] if set(by) >= set(SECTIONS) else out

    # ---- LLM path -----------------------------------------------------
    def write(self, topic: dict, research: dict, video_id: int | None = None) -> dict:
        if self.ai is not None:
            try:
                return self._write_llm(topic, research, video_id)
            except ValidationFailed:
                raise
            except Exception as e:
                if not self.cfg.get("ai.allow_extractive_fallback", True):
                    raise
                log.warning("LLM script writing unavailable (%s) — using extractive fallback", e)
        elif not self.cfg.get("ai.allow_extractive_fallback", True):
            raise ValidationFailed("no AI provider and extractive fallback disabled")
        return self._write_extractive(topic, research)

    def _write_llm(self, topic: dict, research: dict, video_id) -> dict:
        t_min, t_max = self.validator.target_words()
        tgt = self.validator.target()
        attempts = int(self.cfg.get("content.max_script_attempts", 3))
        feedback = ""
        last_validation, last_fc = {}, {}
        facts = [{"claim": f["claim"], "status": f["status"]} for f in research["facts"]]
        for n in range(1, attempts + 1):
            prompt = render_prompt(
                "script", channel_name=self.cfg.get("channel.name", ""), style=self.cfg.get("channel.content_style", ""),
                narration_style=self.cfg.get("channel.narration_style", ""), audience=self.cfg.get("channel.audience", ""),
                language=self.cfg.get("channel.language", "en"), topic_title=topic["title"], facts=facts,
                open_questions=research.get("open_questions", []), target_words=str(t_min), max_words=str(t_max),
                target_seconds=str(int(tgt["target"])), max_hook_words=str(self.cfg.get("content.max_hook_words", 18)),
                max_sentence_words=str(self.cfg.get("content.max_sentence_words", 26)), feedback=feedback)
            data, resp = self.ai.generate_json(prompt, kind="script", video_id=video_id)
            sections = self._clean_sections(data.get("sections") if isinstance(data, dict) else None)
            val = self.validator.validate(sections)
            narration = " ".join(s["text"] for s in sections)
            fc = self.fact_checker.check(narration, research, video_id) if narration else {"passed": False, "issues": []}
            last_validation, last_fc = val, fc
            if val["passed"] and fc["passed"]:
                log.info("script accepted on attempt %d (%s words, ≈%ss)", n, val["words"], val["duration"])
                return self._package(sections, f"llm", resp.provider, val, fc,
                                     (data.get("uncertainty_notes") or "") if isinstance(data, dict) else "",
                                     data.get("pronunciation") if isinstance(data, dict) else None)
            problems = [f"- {i['type']}: {i['detail']}" + (f" (sentence: \"{truncate(i.get('sentence', ''), 80)}\")" if i.get("sentence") else "")
                        for i in (val["issues"] + fc["issues"]) if i["severity"] == "error"]
            feedback = ("\nPREVIOUS ATTEMPT WAS REJECTED. Fix ALL of these problems and keep everything else valid:\n"
                        + "\n".join(problems[:12]) + "\n")
            log.warning("script attempt %d rejected: %d problem(s)", n, len(problems))
        raise ValidationFailed(f"script failed validation after {attempts} attempts",
                               (last_validation.get("issues", []) + last_fc.get("issues", [])))

    # ---- extractive fallback ------------------------------------------
    def _write_extractive(self, topic: dict, research: dict) -> dict:
        facts = research["facts"]
        confirmed = [f for f in facts if f["status"] == "confirmed"]
        hedged = [f for f in facts if f["status"] != "confirmed"]

        def lead(f):  # first sentence, verbatim
            return split_sentences(f["claim"])[0]

        pool = confirmed + hedged
        title = re.sub(r"[?.!]+$", "", topic["title"])
        hook = truncate(title, 110) + "."
        if word_count(hook) > int(self.cfg.get("content.max_hook_words", 18)):
            hook = "Here is what scientists have just reported."
        question = "So what do we actually know?"
        picks = [lead(f) for f in pool[:5]]
        while len(picks) < 3:
            picks.append("The sources describe this finding in more detail, and some questions remain open.")
        oq = (research.get("open_questions") or [None])[0]
        sections = [
            {"section": "hook", "text": hook, "visual": "wide cosmic establishing shot", "sfx": "low_boom"},
            {"section": "question", "text": question, "visual": "slow drift through stars", "sfx": "none"},
            {"section": "story", "text": " ".join(picks[:2]), "visual": "visuals matching the reported subject", "sfx": "shimmer"},
            {"section": "escalation", "text": picks[2], "visual": "closer look at the subject", "sfx": "riser"},
            {"section": "payoff", "text": picks[3] if len(picks) > 3 else picks[2], "visual": "key moment of the finding", "sfx": "low_boom"},
            {"section": "ending", "text": (oq if oq else "Scientists are still working to understand what this really means.")
             , "visual": "pull back to the vastness of space", "sfx": "shimmer"},
        ]
        # trim to the duration window by dropping words from the end of long sections
        t_min, t_max = self.validator.target_words()
        while sum(word_count(s["text"]) for s in sections) > t_max:
            longest = max(sections[2:5], key=lambda s: word_count(s["text"]))
            sents = split_sentences(longest["text"])
            if len(sents) > 1:
                longest["text"] = " ".join(sents[:-1])
            else:
                longest["text"] = truncate(longest["text"], max(60, len(longest["text"]) - 40)).rstrip("…") + "."
        val = self.validator.validate(sections)
        narration = " ".join(s["text"] for s in sections)
        fc = self.fact_checker.check(narration, research, use_llm=False)
        return self._package(sections, "extractive", None, val, fc,
                             "Assembled verbatim from source sentences without an AI writer; review before publishing.")
