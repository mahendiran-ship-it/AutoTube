"""YouTube metadata: honest titles (several options, best chosen by rules), description with sources,
limited hashtags, tags, category, thumbnail text. LLM optional; deterministic fallback."""
from __future__ import annotations

import re

from app.ai.base import render_prompt
from app.core.logger import get_logger
from app.utils.text import jaccard, keywords, split_sentences, tokens, truncate, word_count

log = get_logger("seo")


class SEOGenerator:
    def __init__(self, cfg, ai=None):
        self.cfg, self.ai = cfg, ai

    # ---- title rules --------------------------------------------------
    def _banned_hit(self, text: str) -> str | None:
        t = text.lower()
        for p in self.cfg.get("seo.banned_title_phrases") or []:
            if p.lower() in t:
                return p
        return None

    def title_issues(self, title: str) -> list[str]:
        issues = []
        mx = int(self.cfg.get("seo.title_max_chars", 90))
        if len(title) > mx:
            issues.append(f"longer than {mx} chars")
        if len(title) < 12:
            issues.append("too short")
        hit = self._banned_hit(title)
        if hit:
            issues.append(f"banned phrase '{hit}'")
        letters = [c for c in title if c.isalpha()]
        if letters and sum(c.isupper() for c in letters) / len(letters) > 0.5 and len(letters) > 8:
            issues.append("mostly capitals")
        if title.count("!") > 1 or "?!" in title:
            issues.append("excessive punctuation")
        if re.search(r"[\U0001F300-\U0001FAFF]", title):
            issues.append("emoji")
        return issues

    def score_title(self, title: str, narration: str) -> float:
        """Prefer titles that are curiosity-driven AND grounded in the narration."""
        if self.title_issues(title):
            return -1.0
        s = 5.0
        grounded = jaccard(tokens(title), tokens(narration))
        s += min(2.0, grounded * 8)
        if "?" in title:
            s += 1.0
        if re.search(r"\b(why|how|what|inside|secret|mystery|unknown|never)\b", title, re.I):
            s += 1.0
        if 35 <= len(title) <= 70:
            s += 1.0
        return round(s, 2)

    def _fallback_titles(self, topic: dict) -> list[str]:
        base = re.sub(r"[.!]+$", "", topic["title"]).strip()
        kws = keywords(topic["title"], 3)
        subject = " ".join(kws[:2]).title() if kws else "This Cosmic Mystery"
        cands = [base, f"What We Actually Know About {subject}", f"The Mystery of {subject}", f"Why {subject} Puzzles Scientists"]
        seen, out = set(), []
        for c in cands:
            c = truncate(c, int(self.cfg.get("seo.title_max_chars", 90)))
            if c.lower() not in seen:
                seen.add(c.lower())
                out.append(c)
        return out

    # ---- generation ---------------------------------------------------
    def generate(self, topic: dict, script: dict, research: dict, video_id: int | None = None,
                 synthetic_media: bool = False) -> dict:
        narration = script["narration"]
        llm = None
        if self.ai is not None:
            try:
                prompt = render_prompt("seo", channel_name=self.cfg.get("channel.name", ""), niche=self.cfg.get("channel.niche", ""),
                                       topic_title=topic["title"], narration=narration,
                                       title_options=str(self.cfg.get("seo.title_options", 5)),
                                       title_max=str(self.cfg.get("seo.title_max_chars", 90)),
                                       max_hashtags=str(self.cfg.get("seo.max_hashtags", 3)))
                llm, _ = self.ai.generate_json(prompt, kind="seo", video_id=video_id, temperature=0.6)
                if not isinstance(llm, dict):
                    llm = None
            except Exception as e:
                log.warning("LLM SEO unavailable (%s) — deterministic metadata", e)
        raw_titles = [str(t).strip() for t in (llm or {}).get("titles", []) if str(t).strip()] or self._fallback_titles(topic)
        scored = sorted(((self.score_title(t, narration), t) for t in raw_titles), reverse=True)
        valid = [(s, t) for s, t in scored if s >= 0]
        if not valid:
            valid = [(self.score_title(t, narration), t) for t in self._fallback_titles(topic)]
            valid = [(s, t) for s, t in valid if s >= 0] or [(0.0, truncate(topic["title"], 90))]
        title = valid[0][1]
        options = [{"title": t, "score": s} for s, t in scored]

        kw = [k.lower() for k in (llm or {}).get("keywords", []) if isinstance(k, str)] or keywords(topic["title"] + " " + narration, 8)
        kw = [k for k in dict.fromkeys(kw) if 2 < len(k) < 40][:10]
        topic_tokens = set(tokens(topic["title"] + " " + narration))
        kw = [k for k in kw if set(tokens(k)) & topic_tokens] or keywords(topic["title"], 5)   # no unrelated keyword stuffing

        base_tags = [h.lstrip("#") for h in self.cfg.get("channel.hashtags.base", [])]
        max_h = int(self.cfg.get("seo.max_hashtags", 3))
        hashtags = []
        for h in (llm or {}).get("hashtags", []) + [f"#{b}" for b in base_tags]:
            h = "#" + re.sub(r"[^A-Za-z0-9]", "", str(h))
            if len(h) > 2 and h.lower() not in [x.lower() for x in hashtags] and h.lower() != "#shorts":
                hashtags.append(h)
        hashtags = hashtags[:max(0, max_h - 1)] + [self.cfg.get("channel.hashtags.shorts", "#Shorts")]

        tags: list[str] = []
        budget = int(self.cfg.get("seo.max_tags_chars", 450))
        for t in kw + base_tags + [topic.get("category", "").replace("_", " ")]:
            t = (t or "").strip()
            if t and t.lower() not in [x.lower() for x in tags] and sum(len(x) + 1 for x in tags) + len(t) < budget:
                tags.append(t)

        thumb = (llm or {}).get("thumbnail_text") or ""
        thumb = " ".join(str(thumb).split()[: int(self.cfg.get("thumbnail.max_words", 5))]).strip()
        if not thumb or self._banned_hit(thumb):
            thumb = " ".join(re.sub(r"[?.!]+$", "", title).split()[: int(self.cfg.get("thumbnail.max_words", 5))])
        summary = (llm or {}).get("summary") or (split_sentences(narration)[0] if narration else "")

        description = self._description(summary, research, hashtags, synthetic_media)
        return {"title": title, "title_options": options, "description": description, "hashtags": hashtags,
                "tags": tags, "keywords": kw, "category_id": str(self.cfg.get("youtube.category_id", "28")),
                "thumbnail_text": thumb.upper(), "language": self.cfg.get("youtube.default_language", "en"),
                "made_for_kids": bool(self.cfg.get("youtube.made_for_kids", False)),
                "contains_synthetic_media": synthetic_media, "used_llm": llm is not None}

    def _description(self, summary: str, research: dict, hashtags: list[str], synthetic: bool) -> str:
        lines = [truncate(summary.strip(), 300), ""]
        srcs = [s for s in research.get("sources", []) if s.get("url")]
        if srcs:
            lines.append("Sources:")
            for s in srcs[:5]:
                lines.append(f"- {truncate(s.get('publisher') or s.get('title') or 'Source', 60)}: {s['url']}")
            lines.append("")
        if research.get("warnings"):
            lines.append("Note: some details are still uncertain or debated among scientists.")
            lines.append("")
        if self.cfg.get("seo.include_ai_disclosure_in_description", True):
            lines.append("Narration is AI-generated" + ("; some visuals are AI-generated or procedurally rendered." if synthetic
                                                       else "; visuals are licensed stock/agency media and original animations."))
        lines.append(self.cfg.get("channel.description_footer", ""))
        lines += ["", " ".join(hashtags)]
        return "\n".join(l for l in lines if l is not None).strip()[:4900]

    def validate(self, meta: dict) -> list[str]:
        problems = []
        if self.title_issues(meta.get("title", "")):
            problems += [f"title: {i}" for i in self.title_issues(meta["title"])]
        if len(meta.get("description", "")) < 40:
            problems.append("description too short")
        if len([h for h in meta.get("hashtags", [])]) > int(self.cfg.get("seo.max_hashtags", 3)):
            problems.append("too many hashtags")
        if not meta.get("tags"):
            problems.append("no tags")
        return problems
