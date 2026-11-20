"""Text helpers shared by research, scoring, scripting and subtitles."""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from difflib import SequenceMatcher

STOPWORDS = set("""a an the of to in on at for and or but with from by as is are was were be been being it its this that these
those how what why when where who which about into over after before than then so can could will would may might new
says say said study scientists astronomers researchers find finds found reveal reveals revealed has have had not no
more most some any just also up out off per via their them they you your we our us he she his her i if while during
between across under again against new first one two""".split())

_ABBREV = ("Dr.", "Mr.", "Mrs.", "Ms.", "Prof.", "St.", "vs.", "e.g.", "i.e.", "U.S.", "approx.", "Fig.", "No.", "Jr.", "Sr.")


def normalize(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"[^a-z0-9\s'-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def tokens(text: str) -> list[str]:
    return [w for w in normalize(text).split() if len(w) > 2 and w not in STOPWORDS]


def keywords(text: str, n: int = 8) -> list[str]:
    toks = tokens(text)
    counts = Counter(toks)
    ranked = sorted(counts, key=lambda w: (-counts[w], -len(w), w))
    return ranked[:n]


def fingerprint(title: str) -> str:
    return hashlib.sha1(" ".join(sorted(set(tokens(title)))).encode()).hexdigest()


def jaccard(a: list[str] | set[str], b: list[str] | set[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def title_similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    j = jaccard(ta, tb)
    seq = SequenceMatcher(None, normalize(a), normalize(b)).ratio()
    return max(j, seq * 0.9)


def split_sentences(text: str) -> list[str]:
    t = re.sub(r"\s+", " ", text or "").strip()
    for ab in _ABBREV:
        t = t.replace(ab, ab.replace(".", "․"))
    parts = re.split(r"(?<=[.!?])[\"')\]]*\s+(?=[\"'(\[]?[A-Z0-9])", t)
    return [p.replace("․", ".").strip() for p in parts if p.strip()]


def word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’.-]*", text or ""))


def truncate(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def title_case_clean(title: str) -> str:
    t = re.sub(r"\s+", " ", title or "").strip(" -–|:")
    t = re.sub(r"\s*[-–|]\s*(NASA|ESA|Space\.com|ScienceDaily|Phys\.org|Universe Today).*$", "", t, flags=re.I)
    return t


NUMBER_RE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w])")


def numbers_in(text: str) -> list[str]:
    return [m.group(0).replace(",", "") for m in NUMBER_RE.finditer(text or "")]
