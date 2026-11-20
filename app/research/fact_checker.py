"""Fact checking: deterministic checks always run; an LLM judge is an extra layer when available.

Deterministic checks:
  * every number in the narration must appear in the research record,
  * sentences derived from theory/speculation facts must contain hedging,
  * certainty words (proven, definitely...) are forbidden outside confirmed facts,
  * fabricated-attribution patterns ("NASA announced", "Dr. X said") must be backed by the sources,
  * quote marks require the quoted text to exist in the sources.
"""
from __future__ import annotations

import re

from app.ai.base import render_prompt
from app.core.logger import get_logger
from app.utils.text import jaccard, normalize, numbers_in, split_sentences, tokens, truncate

log = get_logger("factcheck")

CERTAINTY = re.compile(r"\b(proven|proves?|definitely|certainly|undeniabl[ey]|without a doubt|absolutely|guaranteed|"
                       r"we now know for sure|100%|undoubtedly)\b", re.I)
HEDGE = re.compile(r"\b(may|might|could|perhaps|possibly|suspect|scientists think|one idea|one theory|some think|"
                   r"believed|thought to|nobody knows|no one knows|not yet|unclear|unknown|appears?|seems?|"
                   r"it is possible|hypothes|candidate|estimated|suggests?|still debated|open question)\b", re.I)
ATTRIBUTION = re.compile(r"\b(NASA|ESA|JAXA|ISRO|SpaceX|ESO|Roscosmos|CERN|JWST|Webb|Hubble|Dr\.|Professor|Prof\.)"
                         r"\s+(announced|said|says|reported|confirmed|revealed|discovered|stated|claims?)\b", re.I)
QUOTE = re.compile(r"[\"“]([^\"”]{12,})[\"”]")
NUM_WORDS = {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "zero", "first", "second"}


def _src_blob(research: dict) -> str:
    parts = [research.get("summary", "")]
    parts += [f.get("claim", "") for f in research.get("facts", [])]
    parts += list((research.get("excerpts") or {}).values())
    parts += research.get("open_questions", [])
    return " ".join(parts)


def _best_fact(sentence: str, facts: list[dict]) -> tuple[dict | None, float]:
    st = set(tokens(sentence))
    best, score = None, 0.0
    for f in facts:
        s = jaccard(st, set(tokens(f.get("claim", ""))))
        if s > score:
            best, score = f, s
    return best, score


class FactChecker:
    def __init__(self, cfg, ai=None):
        self.cfg, self.ai = cfg, ai

    def deterministic(self, narration: str, research: dict) -> list[dict]:
        issues: list[dict] = []
        blob = _src_blob(research)
        blob_norm = normalize(blob)
        blob_nums = set(numbers_in(blob))
        facts = research.get("facts", [])
        for i, s in enumerate(split_sentences(narration)):
            # numbers must come from the research
            for n in numbers_in(s):
                if n not in blob_nums and n.rstrip("0").rstrip(".") not in {x.rstrip("0").rstrip(".") for x in blob_nums}:
                    issues.append({"index": i, "severity": "error", "type": "unsupported_number",
                                   "detail": f"number '{n}' does not appear in the research record", "sentence": s})
            fact, sim = _best_fact(s, facts)
            status = fact.get("status") if fact and sim >= 0.2 else None
            if status in ("theory", "speculation", "uncertain") and not HEDGE.search(s):
                issues.append({"index": i, "severity": "error", "type": "unhedged_uncertain_claim",
                               "detail": f"sentence matches a '{status}' fact but is stated as certain", "sentence": s})
            if CERTAINTY.search(s) and status != "confirmed":
                issues.append({"index": i, "severity": "error", "type": "overstated_certainty",
                               "detail": "certainty wording used for a claim not marked 'confirmed'", "sentence": s})
            m = ATTRIBUTION.search(s)
            if m and normalize(m.group(1)) not in blob_norm:
                issues.append({"index": i, "severity": "error", "type": "unsourced_attribution",
                               "detail": f"'{m.group(0)}' is not supported by the sources", "sentence": s})
            for q in QUOTE.findall(s):
                if normalize(q) not in blob_norm:
                    issues.append({"index": i, "severity": "error", "type": "unverified_quote",
                                   "detail": "quoted text not found in sources", "sentence": s})
            # a factual sentence with no link at all to the research (and not rhetorical) is suspicious
            if status is None and re.search(r"\b\d", s) and not numbers_in(s):
                pass
        return issues

    def llm_judge(self, narration: str, research: dict, video_id: int | None = None) -> list[dict]:
        sents = split_sentences(narration)
        prompt = render_prompt(
            "factcheck",
            facts=[{"claim": f["claim"], "status": f["status"], "sources": f.get("sources")} for f in research.get("facts", [])],
            excerpts={k: truncate(v, 1200) for k, v in (research.get("excerpts") or {}).items()},
            sentences="\n".join(f"{i}: {s}" for i, s in enumerate(sents)))
        data, _ = self.ai.generate_json(prompt, kind="factcheck", video_id=video_id, temperature=0.1)
        out = []
        for v in (data.get("verdicts") if isinstance(data, dict) else []) or []:
            if v.get("verdict") in ("unsupported", "overstated"):
                idx = int(v.get("index", -1))
                out.append({"index": idx, "severity": "error", "type": f"llm_{v['verdict']}",
                            "detail": str(v.get("note", ""))[:200],
                            "sentence": sents[idx] if 0 <= idx < len(sents) else ""})
        return out

    def check(self, narration: str, research: dict, video_id: int | None = None, use_llm: bool = True) -> dict:
        issues = self.deterministic(narration, research)
        layers = ["deterministic"]
        if use_llm and self.ai is not None:
            try:
                issues += self.llm_judge(narration, research, video_id)
                layers.append("llm")
            except Exception as e:
                log.warning("LLM fact-check layer unavailable: %s", e)
        errors = [i for i in issues if i["severity"] == "error"]
        n_sent = max(1, len(split_sentences(narration)))
        flagged = {i["index"] for i in errors}
        return {"passed": not errors, "issues": issues, "layers": layers,
                "supported_ratio": round(1 - len(flagged) / n_sent, 2),
                "sources_used": len(research.get("sources", [])),
                "warnings": research.get("warnings", [])}
