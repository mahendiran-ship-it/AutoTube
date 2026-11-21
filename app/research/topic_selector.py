"""Duplicate detection, transparent topic scoring (LLM or heuristic), and topic selection."""
from __future__ import annotations

import re

from app.ai.base import render_prompt
from app.core.errors import ProviderUnavailable, ValidationFailed
from app.core.logger import get_logger
from app.database.database import utcnow
from app.generation.visual_kinds import kind_scores, visual_support
from app.utils.text import jaccard, keywords, title_similarity, tokens, truncate

log = get_logger("selector")

CRITERIA = ["curiosity", "audience_interest", "visual_potential", "scientific_importance", "mystery",
            "emotional_impact", "relevance", "originality", "competition", "available_visuals", "retention_potential"]
LABELS = {"curiosity": "Curiosity", "audience_interest": "Audience interest", "visual_potential": "Visual potential",
          "scientific_importance": "Scientific importance", "mystery": "Mystery", "emotional_impact": "Emotional impact",
          "relevance": "Relevance", "originality": "Originality", "competition": "Competition (10 = little)",
          "available_visuals": "Available visuals", "retention_potential": "Retention potential"}

_CURIOUS = ["why", "how ", "mystery", "unexplained", "strange", "unexpected", "never", "hidden", "unknown", "puzzl",
            "surpris", "anomal", "bizarre", "secret", "rare", "record", "mysterious", "baffl"]
_MYSTERY = ["unexplained", "unknown", "mystery", "puzzl", "anomal", "unclear", "baffl", "enigma", "strange",
            "mysterious", "origin", "no one knows", "why", "can't explain", "cannot explain"]
_IMPORTANT = ["discover", "detect", "first", "mission", "telescope", "webb", "jwst", "hubble", "evidence", "confirm",
              "measure", "observ", "survey", "data"]
_EMOTION = ["vast", "end ", "death", "collapse", "collision", "billion", "light-year", "light year", "edge", "largest",
            "smallest", "oldest", "extreme", "violent", "dying", "birth", "tear", "swallow"]
_GENERIC = ["top 10", "top ten", "facts about", "things you", "you need to know", "list of"]


def _clamp(x: float, hi: float = 10.0) -> float:
    return round(max(1.0, min(hi, x)), 1)


def _hits(text: str, words: list[str]) -> int:
    t = text.lower()
    return sum(1 for w in words if w in t)


class DuplicateDetector:
    def __init__(self, repo, cfg):
        self.repo, self.cfg = repo, cfg

    @property
    def threshold(self) -> float:
        return float(self.cfg.get("research.duplicate_similarity_threshold", 0.55))

    def corpus(self) -> list[dict]:
        rows = [{"kind": "topic", "id": t["id"], "title": t["title"],
                 "kw": t.get("keywords") or keywords(t["title"] + " " + (t.get("summary") or ""))}
                for t in self.repo.topics_for_dedup()]
        for v in self.repo.list_videos(limit=500):
            if v.get("title") and not v.get("is_smoke") and v["status"] not in ("rejected", "failed"):
                rows.append({"kind": "video", "id": v["id"], "title": v["title"], "kw": keywords(v["title"])})
        return rows

    def similarity(self, title: str, summary: str = "", kw: list[str] | None = None,
                   exclude_topic_id: int | None = None) -> tuple[float, dict | None]:
        kw = kw or keywords(title + " " + summary)
        best, match = 0.0, None
        for c in self.corpus():
            if c["kind"] == "topic" and c["id"] == exclude_topic_id:
                continue
            s = max(title_similarity(title, c["title"]), 0.9 * jaccard(kw, c["kw"] or []))
            if s > best:
                best, match = s, c
        return round(best, 3), match

    def check(self, title: str, summary: str = "", kw: list[str] | None = None,
              exclude_topic_id: int | None = None) -> tuple[bool, float, dict | None]:
        s, m = self.similarity(title, summary, kw, exclude_topic_id)
        return s >= self.threshold, s, m


class TopicScorer:
    def __init__(self, cfg, ai=None):
        self.cfg, self.ai = cfg, ai

    @property
    def weights(self) -> dict:
        return self.cfg.get("scoring.weights") or {}

    def total(self, crit: dict) -> float:
        num = den = 0.0
        for k, v in crit.items():
            if v is None or k not in self.weights:
                continue
            w = float(self.weights[k])
            num += w * float(v)
            den += w
        return round(num / den, 2) if den else 0.0

    # ---- deterministic, transparent heuristic ------------------------
    def heuristic(self, topic: dict, max_history_similarity: float = 0.0, has_history: bool = True) -> dict:
        text = f"{topic['title']} {topic.get('summary') or ''}"
        sig = topic.get("signal_data") or {}
        trend = (sig.get("trend") or {}) if isinstance(sig, dict) else {}
        coverage = int(sig.get("coverage", 1)) if isinstance(sig, dict) else 1
        rel = {"high": 1.5, "medium": 0.5}.get(sig.get("reliability") if isinstance(sig, dict) else None, 0.0)
        kinds = sum(kind_scores(text).values())
        cat = topic.get("category")
        cat_hits = 0
        if cat:
            cat_hits = _hits(text, [w.lower() for w in (self.cfg.get(f"channel.categories.{cat}") or [])])
        audience = 4.5 + min(2.0, (coverage - 1) * 0.8)
        if trend:
            audience += min(2.5, max(0.0, trend.get("ratio", 1) - 1))
        generic = _hits(text, _GENERIC)
        return {
            "curiosity": _clamp(4 + 1.0 * _hits(text, _CURIOUS) + (0.5 if "?" in topic["title"] else 0) - generic, 9),
            "audience_interest": _clamp(audience, 9),
            "visual_potential": _clamp(4 + 1.2 * kinds, 9),
            "scientific_importance": _clamp(4 + rel + 0.8 * _hits(text, _IMPORTANT), 9),
            "mystery": _clamp(3.5 + 1.3 * _hits(text, _MYSTERY), 9),
            "emotional_impact": _clamp(4 + 1.0 * _hits(text, _EMOTION), 9),
            "relevance": _clamp(7 + 0.5 * cat_hits, 10) if cat else 3.0,
            "originality": _clamp(10 - 9 * max_history_similarity - 2 * generic, 9.5) if has_history else 7.0,
            "competition": None,                      # not measurable from free sources → excluded, not faked
            "available_visuals": _clamp(4 + 5 * visual_support(text), 9),
            "retention_potential": _clamp(4 + (1 if _hits(text, _MYSTERY) else 0) + (1 if re.search(r"\d", text) else 0)
                                          + (1 if len(topic.get("summary") or "") > 200 else 0)
                                          + 0.8 * min(2, _hits(text, _CURIOUS)), 9),
        }

    # ---- LLM scoring --------------------------------------------------
    def llm_scores(self, topics: list[dict], video_id: int | None = None) -> dict[int, dict]:
        if not self.ai:
            raise ProviderUnavailable("no AI manager")
        payload = [{"id": t["id"], "title": t["title"], "summary": truncate(t.get("summary") or "", 450),
                    "source": t.get("source_name"), "category": t.get("category"),
                    "signal": (t.get("signal_type") or "research_signal")} for t in topics]
        prompt = render_prompt("topic_scoring", channel_name=self.cfg.get("channel.name", ""),
                               niche=self.cfg.get("channel.niche", ""), style=self.cfg.get("channel.content_style", ""),
                               candidates=payload)
        data, resp = self.ai.generate_json(prompt, kind="topic_scoring", video_id=video_id)
        rows = data.get("scores") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise ValidationFailed("topic scoring JSON missing 'scores'")
        out: dict[int, dict] = {}
        for r in rows:
            try:
                tid = int(r["id"])
            except (KeyError, TypeError, ValueError):
                continue
            crit = {}
            for k in CRITERIA:
                v = r.get(k)
                if v is None:
                    crit[k] = None
                    continue
                try:
                    crit[k] = float(max(1, min(10, float(v))))
                except (TypeError, ValueError):
                    crit[k] = None
            if sum(1 for v in crit.values() if v is not None) >= 8:
                out[tid] = {"criteria": crit, "reason": str(r.get("reason", ""))[:400], "provider": resp.provider}
        return out


class TopicSelector:
    def __init__(self, cfg, repo, scorer: TopicScorer, dedup: DuplicateDetector, notifier=None):
        self.cfg, self.repo, self.scorer, self.dedup, self.notifier = cfg, repo, scorer, dedup, notifier

    def topic_weights(self) -> dict:
        return self.repo.get_setting("topic_weights", {}) or {}

    def _blocked(self, topic: dict) -> str | None:
        t = (topic["title"] + " " + (topic.get("summary") or "")).lower()
        for term in self.cfg.get("channel.blocked_terms") or []:
            if term.lower() in t:
                return term
        return None

    def _recent_categories(self, n: int = 2) -> list[str]:
        cats = []
        for v in self.repo.list_videos(limit=n):
            if v.get("topic_id"):
                t = self.repo.get("topics", v["topic_id"])
                if t and t.get("category"):
                    cats.append(t["category"])
        return cats

    def evaluate(self, topics: list[dict], use_llm: bool = True) -> list[dict]:
        """Return scored entries (not persisted): {topic, criteria, total, method, reason, ...}."""
        llm: dict[int, dict] = {}
        method = "heuristic"
        if use_llm and self.scorer.ai is not None:
            try:
                llm = self.scorer.llm_scores(topics)
                method = "llm"
            except Exception as e:
                log.warning("LLM topic scoring unavailable (%s) — using transparent heuristic", e)
        weights = self.topic_weights()
        recent = self._recent_categories()
        has_history = bool(self.dedup.corpus())
        out = []
        for t in topics:
            sim, match = self.dedup.similarity(t["title"], t.get("summary") or "", t.get("keywords"), t["id"])
            h = self.scorer.heuristic(t, sim, has_history)
            if t["id"] in llm:
                crit = dict(llm[t["id"]]["criteria"])
                crit["originality"] = min(crit.get("originality") or 10, h["originality"])   # history is ground truth
                m, reason = "llm", llm[t["id"]]["reason"]
            else:
                crit, m, reason = h, "heuristic", ""
            base = self.scorer.total(crit)
            cat_w = float(weights.get(t.get("category") or "", 1.0))
            variety = 0.93 if t.get("category") and recent.count(t.get("category")) >= 2 else 1.0
            final = round(base * cat_w * variety, 2)
            out.append({"topic": t, "criteria": crit, "base_total": base, "category_weight": cat_w,
                        "variety_factor": variety, "total": final, "method": m, "llm_reason": reason,
                        "closest_history": ({"title": match["title"], "similarity": sim} if match else None)})
        return sorted(out, key=lambda e: -e["total"])

    def explain(self, e: dict) -> str:
        crit = {k: v for k, v in e["criteria"].items() if v is not None}
        top = sorted(crit.items(), key=lambda kv: -kv[1])[:3]
        low = sorted(crit.items(), key=lambda kv: kv[1])[:1]
        t = e["topic"]
        parts = [f"Score {e['total']}/10 ({e['method']} scoring).",
                 "Strongest: " + ", ".join(f"{LABELS[k]} {v:g}" for k, v in top) + ".",
                 "Weakest: " + ", ".join(f"{LABELS[k]} {v:g}" for k, v in low) + "."]
        sig = t.get("signal_data") or {}
        if t.get("signal_type") == "trend" and isinstance(sig, dict) and sig.get("trend"):
            tr = sig["trend"]
            parts.append(f"Measured attention: Wikipedia pageviews for '{tr['article']}' are {tr['ratio']}x their recent baseline.")
        else:
            cov = sig.get("coverage", 1) if isinstance(sig, dict) else 1
            parts.append(f"Research signal only (not a measured trend); covered by {cov} source(s).")
        if e["closest_history"]:
            parts.append(f"Closest previous topic: '{e['closest_history']['title']}' (similarity {e['closest_history']['similarity']}).")
        else:
            parts.append("No similar topic exists in the history.")
        if e["category_weight"] != 1.0:
            parts.append(f"Performance weight for '{t.get('category')}' = {e['category_weight']}.")
        if e["llm_reason"]:
            parts.append(e["llm_reason"])
        return " ".join(parts)

    def select(self, candidates: list[dict] | None = None, use_llm: bool = True) -> dict:
        cands = candidates if candidates is not None else self.repo.recent_candidates(
            int(self.cfg.get("research.max_item_age_days", 21)))
        rejected: list[dict] = []
        eligible: list[dict] = []
        for t in cands:
            term = self._blocked(t)
            if term:
                self._reject(t, f"blocked term: '{term}'")
                rejected.append({"id": t["id"], "title": t["title"], "reason": f"blocked term '{term}'"})
                continue
            dup, sim, match = self.dedup.check(t["title"], t.get("summary") or "", t.get("keywords"), t["id"])
            if dup:
                reason = f"duplicate of \"{match['title']}\" (similarity {sim})"
                self._reject(t, reason)
                rejected.append({"id": t["id"], "title": t["title"], "reason": reason})
                continue
            eligible.append(t)
        if not eligible:
            return {"selected": None, "reason": "no eligible candidates (all duplicates/blocked or none discovered)",
                    "evaluated": [], "rejected": rejected}
        # cheap pre-ranking so the LLM only judges the most promising N
        n = int(self.cfg.get("research.max_candidates_evaluated", 12))
        pre = sorted(eligible, key=lambda t: -self.scorer.total(self.scorer.heuristic(t)))[:n]
        scored = self.evaluate(pre, use_llm=use_llm)
        floor = float(self.cfg.get("scoring.min_total_score", 4.5))
        best = scored[0]
        if best["total"] < floor:
            return {"selected": None, "reason": f"best candidate scored {best['total']} < minimum {floor}",
                    "evaluated": [self._summ(e) for e in scored], "rejected": rejected}
        reason = self.explain(best)
        self._mark_selected(best, reason)
        for e in scored[1:]:
            self._reject(e["topic"], f"lower score ({e['total']}) than selected topic #{best['topic']['id']} ({best['total']})",
                         e)
        return {"selected": self.repo.get("topics", best["topic"]["id"]), "reason": reason,
                "evaluated": [self._summ(e) for e in scored], "rejected": rejected, "method": best["method"]}

    def select_specific(self, topic_id: int, use_llm: bool = True) -> dict:
        t = self.repo.get("topics", topic_id)
        if not t:
            raise ValueError(f"topic {topic_id} not found")
        dup, sim, match = self.dedup.check(t["title"], t.get("summary") or "", t.get("keywords"), t["id"])
        if dup:
            return {"selected": None, "reason": f"duplicate of \"{match['title']}\" (similarity {sim})",
                    "evaluated": [], "rejected": []}
        e = self.evaluate([t], use_llm=use_llm)[0]
        self._mark_selected(e, "Chosen manually. " + self.explain(e))
        return {"selected": self.repo.get("topics", topic_id), "reason": "manual", "evaluated": [self._summ(e)],
                "rejected": [], "method": e["method"]}

    # ---- persistence --------------------------------------------------
    def _score_blob(self, e: dict) -> dict:
        return {"criteria": e["criteria"], "base_total": e["base_total"], "category_weight": e["category_weight"],
                "variety_factor": e["variety_factor"], "method": e["method"],
                "closest_history": e["closest_history"], "weights": self.scorer.weights}

    def _mark_selected(self, e: dict, reason: str) -> None:
        self.repo.set_topic_status(e["topic"]["id"], "selected", score_total=e["total"], score_json=self._score_blob(e),
                                   score_method=e["method"], selection_reason=reason, selected_at=utcnow())

    def _reject(self, topic: dict, reason: str, e: dict | None = None) -> None:
        extra = {"score_total": e["total"], "score_json": self._score_blob(e), "score_method": e["method"]} if e else {}
        self.repo.set_topic_status(topic["id"], "rejected", rejection_reason=reason, **extra)

    @staticmethod
    def _summ(e: dict) -> dict:
        return {"id": e["topic"]["id"], "title": e["topic"]["title"], "total": e["total"], "method": e["method"],
                "criteria": e["criteria"]}
