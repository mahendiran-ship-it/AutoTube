"""Mapping narration/topic text -> the kind of procedural visual that can honestly illustrate it."""
from __future__ import annotations

import re

VISUAL_KINDS = ["starfield", "nebula", "planet", "black_hole", "galaxy", "orbit"]

_KEYWORDS: dict[str, list[str]] = {
    "black_hole": ["black hole", "event horizon", "singularity", "accretion", "quasar", "hawking",
                   "spaghettification", "gravitational lens", "lensing", "photon ring"],
    "galaxy": ["galaxy", "galaxies", "milky way", "andromeda", "spiral", "cosmic web", "galactic", "dark matter",
               "cluster"],
    "nebula": ["nebula", "star formation", "stellar nursery", "gas cloud", "dust", "supernova", "pillars",
               "remnant", "birth of stars", "stars are born"],
    "planet": ["planet", "mars", "jupiter", "saturn", "venus", "mercury", "neptune", "uranus", "exoplanet",
               "europa", "titan", "enceladus", "surface", "atmosphere", "world"],
    "orbit": ["orbit", "orbits", "orbiting", "moon", "eclipse", "solar system", "comet", "asteroid",
              "trajectory", "revolve", "satellite", "tidal", "gravity pulls"],
}
_PRIORITY = ["black_hole", "galaxy", "nebula", "planet", "orbit"]

# What stock-footage search should look for per kind.
SEARCH_HINTS = {
    "black_hole": "black hole space", "galaxy": "galaxy stars", "nebula": "nebula space",
    "planet": "planet space", "orbit": "orbit planet moon", "starfield": "stars night sky",
}


def kind_scores(text: str) -> dict[str, int]:
    t = (text or "").lower()
    return {k: sum(len(re.findall(r"\b" + re.escape(w), t)) for w in ws) for k, ws in _KEYWORDS.items()}


def detect_visual_kind(text: str, default: str = "starfield") -> str:
    scores = kind_scores(text)
    best = max(_PRIORITY, key=lambda k: (scores[k], -_PRIORITY.index(k)))
    return best if scores[best] > 0 else default


def visual_support(text: str) -> float:
    """0..1: how well we can illustrate this text with local procedural visuals."""
    s = kind_scores(text)
    hits = sum(s.values())
    return min(1.0, 0.35 + 0.2 * hits) if hits else 0.35
