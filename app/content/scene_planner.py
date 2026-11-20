"""Scene planner: narration -> 5-8 scenes with visual direction. Heuristic by default; LLM optional.

Every scene's visual_kind is derived from ITS OWN narration so visuals match what is being said.
"""
from __future__ import annotations

import re

from app.ai.base import render_prompt
from app.core.logger import get_logger
from app.generation.visual_kinds import SEARCH_HINTS, VISUAL_KINDS, detect_visual_kind
from app.utils.text import keywords, split_sentences, truncate, word_count

log = get_logger("scenes")

CAMERAS = {"hook": "slow push-in", "question": "gentle drift left", "story": "slow pan right",
           "escalation": "accelerating push-in", "payoff": "dramatic pull-back reveal", "ending": "slow pull-back into the dark"}
LIGHTING = {"black_hole": "near-black space, glowing orange accretion light with cold blue highlights",
            "galaxy": "deep indigo space with warm golden galactic core glow",
            "nebula": "rich magenta and teal gas clouds lit by hidden stars",
            "planet": "hard sunlight on one side, deep shadow on the other",
            "orbit": "cold starlight with a thin bright sun edge", "starfield": "pitch-dark space with sparse cold starlight"}
ATMOS = {"hook": "tense, awe-struck", "question": "curious, quiet", "story": "immersive, steady",
         "escalation": "rising tension", "payoff": "revelation, wonder", "ending": "reflective, vast"}
TRANSITIONS = {"hook": "fade", "question": "dissolve", "story": "dissolve", "escalation": "fadeblack",
               "payoff": "dissolve", "ending": "fade"}
_SFX_DEFAULT = {"hook": "low_boom", "payoff": "low_boom", "escalation": "riser", "ending": "shimmer"}


class ScenePlanner:
    def __init__(self, cfg, ai=None):
        self.cfg, self.ai = cfg, ai

    def _bounds(self) -> tuple[int, int]:
        lo = int(self.cfg.get("content.scene_count_min", 5))
        hi = int(self.cfg.get("content.scene_count_max", 8))
        return lo, hi

    # ---- grouping -----------------------------------------------------
    def _sentence_table(self, script: dict) -> list[dict]:
        rows = []
        for sec in script["sections"]:
            for s in split_sentences(sec["text"]):
                rows.append({"text": s, "section": sec["section"], "visual": sec.get("visual", ""), "sfx": sec.get("sfx", "none")})
        return rows

    def _group(self, rows: list[dict], n_scenes: int) -> list[list[int]]:
        """Contiguous grouping into n_scenes groups with roughly equal word counts."""
        n_scenes = max(1, min(n_scenes, len(rows)))
        weights = [max(1, word_count(r["text"])) for r in rows]
        total = sum(weights)
        groups, cur, acc = [], [], 0.0
        remaining_scenes = n_scenes
        for i, w in enumerate(weights):
            cur.append(i)
            acc += w
            sentences_left = len(rows) - i - 1
            if remaining_scenes > 1 and (acc >= total / n_scenes or sentences_left <= remaining_scenes - 1) \
                    and sentences_left >= remaining_scenes - 1:
                groups.append(cur)
                cur, acc = [], 0.0
                remaining_scenes -= 1
                total = sum(weights[i + 1:]) or 1
                n_scenes = remaining_scenes
        if cur:
            groups.append(cur)
        return groups

    def _heuristic_scene(self, idx: int, rows: list[dict], members: list[int], topic_text: str) -> dict:
        texts = [rows[i]["text"] for i in members]
        narration = " ".join(texts)
        section = rows[members[0]]["section"]
        kind = detect_visual_kind(narration, default=detect_visual_kind(topic_text + " " + rows[members[0]]["visual"], "starfield"))
        hint = rows[members[0]]["visual"] or f"cinematic {kind.replace('_', ' ')}"
        kws = keywords(narration, 3)
        desc = f"{truncate(hint, 160)}"
        prompt = (f"Cinematic vertical 9:16 shot, {kind.replace('_', ' ')}, {desc}. {LIGHTING[kind]}. "
                  f"Photorealistic space imagery, volumetric light, film grain, no text, no people, no logos.")
        sfx = next((rows[i]["sfx"] for i in members if rows[i]["sfx"] not in ("none", "")), _SFX_DEFAULT.get(section, "none"))
        return {"idx": idx, "section": section, "narration": narration, "est_duration": 0.0, "visual_description": desc,
                "visual_prompt": prompt, "visual_kind": kind,
                "search_query": (SEARCH_HINTS[kind] if not kws else f"{kws[0]} {SEARCH_HINTS[kind].split()[0]}"),
                "camera": CAMERAS.get(section, "slow push-in"), "lighting": LIGHTING[kind],
                "atmosphere": ATMOS.get(section, "immersive"), "sfx": sfx, "transition": TRANSITIONS.get(section, "dissolve")}

    def _llm_scenes(self, rows, n_scenes, video_id) -> list[dict] | None:
        if not self.ai:
            return None
        try:
            prompt = render_prompt("scenes", scene_count=str(n_scenes), kinds=", ".join(VISUAL_KINDS),
                                   sentences="\n".join(f"{i}: {r['text']}" for i, r in enumerate(rows)))
            data, _ = self.ai.generate_json(prompt, kind="scenes", video_id=video_id, temperature=0.4)
            scenes = data.get("scenes") if isinstance(data, dict) else None
            if not scenes:
                return None
            used: list[int] = []
            for s in scenes:
                used += [int(i) for i in s.get("sentence_indices", [])]
            if sorted(used) != list(range(len(rows))):          # must cover every sentence exactly once, in order
                log.warning("LLM scene plan did not cover sentences exactly once — using heuristic planner")
                return None
            return scenes
        except Exception as e:
            log.warning("LLM scene planning unavailable (%s) — using heuristic planner", e)
            return None

    # ---- main ---------------------------------------------------------
    def plan(self, script: dict, topic: dict, video_id: int | None = None) -> list[dict]:
        rows = self._sentence_table(script)
        if not rows:
            raise ValueError("script has no sentences")
        lo, hi = self._bounds()
        total_words = sum(word_count(r["text"]) for r in rows)
        n = max(lo, min(hi, round(total_words / 17)))      # ~7s/scene
        n = min(n, len(rows))
        topic_text = f"{topic['title']} {topic.get('summary') or ''}"
        llm = self._llm_scenes(rows, n, video_id)
        scenes: list[dict] = []
        if llm:
            for i, s in enumerate(llm):
                members = [int(x) for x in s["sentence_indices"]]
                base = self._heuristic_scene(i, rows, members, topic_text)
                kind = s.get("visual_kind") if s.get("visual_kind") in VISUAL_KINDS else base["visual_kind"]
                base.update({"visual_kind": kind,
                             **{k: str(s[k])[:400] for k in ("visual_description", "visual_prompt", "search_query", "camera",
                                                             "lighting", "atmosphere") if s.get(k)}})
                if s.get("sfx") in ("none", "low_boom", "whoosh", "shimmer", "riser", "heartbeat"):
                    base["sfx"] = s["sfx"]
                if s.get("transition") in ("fade", "dissolve", "fadeblack"):
                    base["transition"] = s["transition"]
                scenes.append(base)
        else:
            for i, members in enumerate(self._group(rows, n)):
                scenes.append(self._heuristic_scene(i, rows, members, topic_text))
        wpm = float(self.cfg.get("content.words_per_minute", 150))
        for s in scenes:
            s["est_duration"] = round(word_count(s["narration"]) / wpm * 60 + 0.3, 2)
        return scenes

    @staticmethod
    def validate(scenes: list[dict], narration: str) -> list[str]:
        problems = []
        if not (3 <= len(scenes) <= 12):
            problems.append(f"unusual scene count {len(scenes)}")
        joined = re.sub(r"\s+", " ", " ".join(s["narration"] for s in scenes)).strip()
        if joined != re.sub(r"\s+", " ", narration).strip():
            problems.append("scene narration does not reproduce the full script in order")
        for s in scenes:
            if not s.get("visual_description"):
                problems.append(f"scene {s['idx']} has no visual description")
        return problems
