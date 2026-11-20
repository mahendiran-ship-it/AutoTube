"""Phases 2-3: AI providers, feeds, discovery, dedup, scoring, fact-check, script, scenes, SEO."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.ai.base import LLMProvider, LLMResponse, extract_json, render_prompt
from app.ai.provider_manager import ProviderManager
from app.content.content_validator import ContentValidator, estimate_duration
from app.content.scene_planner import ScenePlanner
from app.content.script_writer import ScriptWriter
from app.content.seo_generator import SEOGenerator
from app.core.budget import BudgetController
from app.core.errors import BudgetExceeded, ProviderUnavailable, ValidationFailed
from app.research.fact_checker import FactChecker
from app.research.feed_collector import FeedCollector, parse_feed
from app.research.research_engine import ResearchEngine
from app.research.topic_discovery import TopicDiscovery, WikipediaTrends
from app.research.topic_selector import DuplicateDetector, TopicScorer, TopicSelector


# ---------------- fakes ----------------
class FakeProvider(LLMProvider):
    def __init__(self, name="fake", healthy=True, replies=None, paid=False, fail=False):
        self.name, self.healthy, self.replies, self.is_paid, self.fail = name, healthy, list(replies or []), paid, fail
        self.model, self.calls = "m", 0

    def health(self):
        return self.healthy, "ok" if self.healthy else "down"

    def generate(self, prompt, system=None, *, json_mode=False, temperature=0.7, max_tokens=None):
        self.calls += 1
        if self.fail:
            raise ProviderUnavailable("boom")
        text = self.replies.pop(0) if self.replies else "{}"
        return LLMResponse(text=text if isinstance(text, str) else json.dumps(text), model="m", provider=self.name,
                           prompt_tokens=1, completion_tokens=1)


def manager(cfg, repo, provs, order=None):
    cfg.set("ai.fallback_order", order or list(provs))
    return ProviderManager(cfg, repo, BudgetController(cfg, repo), providers=provs)


RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Webb finds a puzzling black hole in the early universe</title><link>https://example.org/a</link>
<description>&lt;p&gt;Astronomers using the James Webb Space Telescope found a black hole that may be too massive for its age.&lt;/p&gt;</description>
<pubDate>{date}</pubDate></item>
<item><title>Odd Mars rock puzzles rover team</title><link>https://example.org/b</link><description>A rock.</description>
<pubDate>{date}</pubDate></item></channel></rss>"""


def rss_now():
    return RSS.format(date=(datetime.now(timezone.utc) - timedelta(days=1)).strftime("%a, %d %b %Y %H:%M:%S +0000")).encode()


# ---------------- providers ----------------
def test_extract_json_handles_fences_and_prose():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here you go: {"a": [1, {"b": "}"}]} thanks') == {"a": [1, {"b": "}"}]}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_prompt_render_fills_vars_and_detects_missing():
    out = render_prompt("seo", channel_name="C", niche="n", topic_title="t", narration="x", title_options="5",
                        title_max="90", max_hashtags="3")
    assert "<<" not in out and '"titles"' in out        # JSON braces untouched
    with pytest.raises(KeyError):
        render_prompt("seo", channel_name="C")


def test_provider_fallback_skips_unhealthy_and_records_usage(cfg, repo):
    a, b = FakeProvider("a", healthy=False), FakeProvider("b", replies=['{"ok": true}'])
    pm = manager(cfg, repo, {"a": a, "b": b})
    data, resp = pm.generate_json("hi")
    assert data == {"ok": True} and resp.provider == "b" and a.calls == 0 and pm.last_provider == "b"
    assert repo.api_usage_summary()[0]["provider"] == "b"


def test_provider_failure_switches_once_and_is_bounded(cfg, repo):
    cfg.set("ai.max_attempts_per_call", 2)
    a, b, c = FakeProvider("a", fail=True), FakeProvider("b", fail=True), FakeProvider("c", replies=["{}"])
    pm = manager(cfg, repo, {"a": a, "b": b, "c": c})
    with pytest.raises(ProviderUnavailable):
        pm.generate("hi")
    assert (a.calls, b.calls, c.calls) == (1, 1, 0)      # never loops, never exceeds the cap


def test_all_unhealthy_raises_clear_error(cfg, repo):
    pm = manager(cfg, repo, {"a": FakeProvider("a", healthy=False)})
    with pytest.raises(ProviderUnavailable, match="No healthy AI provider"):
        pm.generate("hi")
    assert pm.available() is False


def test_paid_provider_blocked_by_zero_budget(cfg, repo):
    pm = manager(cfg, repo, {"p": FakeProvider("p", paid=True, replies=["{}"])})
    with pytest.raises(BudgetExceeded):
        pm.generate("hi")


def test_malformed_json_gets_one_repair_attempt(cfg, repo):
    pm = manager(cfg, repo, {"a": FakeProvider("a", replies=["not json", '{"fixed": 1}'])})
    assert pm.generate_json("x")[0] == {"fixed": 1}
    pm2 = manager(cfg, repo, {"a": FakeProvider("a", replies=["bad", "still bad"])})
    with pytest.raises(ValidationFailed):
        pm2.generate_json("x")


# ---------------- feeds & discovery ----------------
def test_parse_feed_rss_and_atom_and_rejects_entities():
    items = parse_feed(rss_now(), {"id": "x", "name": "X", "reliability": "high"})
    assert len(items) == 2 and "<p>" not in items[0].summary and items[0].reliability == "high"
    atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>T of atom</title><link href="https://e.org/1"/><updated>2026-01-01T00:00:00Z</updated><summary>S</summary></entry></feed>'
    assert parse_feed(atom, {"id": "a"})[0].link == "https://e.org/1"
    with pytest.raises(ValueError):
        parse_feed(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><rss><channel/></rss>', {"id": "x"})


def test_one_failing_source_does_not_stop_others(cfg, repo):
    cfg.set("sources.feeds", [{"id": "bad", "name": "Bad", "url": "https://bad.example/f", "enabled": True},
                              {"id": "good", "name": "Good", "url": "https://good.example/f", "enabled": True},
                              {"id": "off", "name": "Off", "url": "https://off.example/f", "enabled": False}])

    def fetch(url):
        if "bad" in url:
            raise RuntimeError("down")
        return rss_now()
    items, report = FeedCollector(cfg, repo, fetch=fetch).collect()
    st = {r["id"]: r["status"] for r in report}
    assert st == {"bad": "error", "good": "ok", "off": "disabled"} and len(items) == 2
    assert {s["source_key"]: s["last_status"] for s in repo.list_sources()}["bad"] == "error"


def make_discovery(cfg, repo, trends=None):
    cfg.set("sources.feeds", [{"id": "g", "name": "Good", "url": "https://good.example/f", "reliability": "high"}])
    return TopicDiscovery(cfg, repo, FeedCollector(cfg, repo, fetch=lambda u: rss_now()), trends=trends)


def test_discovery_stores_topics_labels_signal_and_dedups(cfg, repo):
    d = make_discovery(cfg, repo)
    r1 = d.discover()
    assert r1["new_topics"] == 2
    topics = repo.list_topics()
    assert all(t["signal_type"] == "research_signal" for t in topics)       # no fake "trend"
    assert any(t["category"] == "black_holes" for t in topics)
    assert make_discovery(cfg, repo).discover()["new_topics"] == 0           # same items → not re-added


def test_blocked_terms_never_become_topics(cfg, repo):
    cfg.set("sources.feeds", [{"id": "g", "name": "G", "url": "https://g.example/f"}])
    xml = rss_now().replace(b"Odd Mars rock puzzles rover team", b"NASA is hiding alien base on Mars revealed")
    d = TopicDiscovery(cfg, repo, FeedCollector(cfg, repo, fetch=lambda u: xml))
    assert d.discover()["blocked"] == 1


def test_trend_label_only_from_measured_spike(cfg, repo):
    def views(recent, base):
        return {"items": [{"views": base}] * 14 + [{"views": recent}] * 3 + [{"views": recent}]}

    def fetch(url):
        return views(5000, 1000) if "Black_hole" in url else views(1000, 1000)
    cfg.set("sources.trend_sources.wikipedia_pageviews.watchlist", ["Black hole", "Dark matter"])
    tr = WikipediaTrends(cfg, fetch=fetch, sleep=lambda s: None)
    rows = tr.measure()
    assert rows[0]["article"] == "Black hole" and rows[0]["ratio"] > 3 and rows[1]["ratio"] == 1.0
    d = make_discovery(cfg, repo, trends=tr)
    d._fetch_json = lambda u: {"extract": "A black hole is a region of spacetime.", "content_urls": {}}
    res = d.discover()
    assert res["trends_spiking"] == 1 and res["trend_topics"] == 1
    assert any(t["signal_type"] == "trend" for t in repo.list_topics())


# ---------------- duplicates & scoring ----------------
def add(repo, title, status="candidate", summary="", **kw):
    from app.utils.text import fingerprint, keywords, normalize
    return repo.add_topic(title=title, normalized_title=normalize(title), fingerprint=fingerprint(title), summary=summary,
                          status=status, keywords=keywords(title + " " + summary, 8), **kw)


def test_duplicate_detection(cfg, repo):
    add(repo, "What happens inside a black hole event horizon", status="published")
    dd = DuplicateDetector(repo, cfg)
    assert dd.check("What really happens inside a black hole's event horizon?")[0] is True
    assert dd.check("Why Saturn's rings are disappearing")[0] is False
    # candidates/rejected topics do not block new ones
    add(repo, "Europa ocean plumes spotted", status="rejected")
    assert dd.check("Europa ocean plumes spotted")[0] is False


def test_scores_are_transparent_bounded_and_competition_excluded(cfg, repo):
    t = {"id": 1, "title": "Why is this black hole mystery so puzzling?", "summary": "A strange unexplained black hole "
         "with 13 billion years of history observed by Webb. " * 3, "category": "black_holes", "signal_type": "research_signal",
         "signal_data": {"coverage": 3, "reliability": "high"}, "source_name": "NASA"}
    h = TopicScorer(cfg).heuristic(t)
    assert set(h) >= {"curiosity", "visual_potential", "originality", "retention_potential"}
    assert h["competition"] is None
    assert all(1 <= v <= 10 for v in h.values() if v is not None)
    weak = TopicScorer(cfg).heuristic({"id": 2, "title": "Top 10 facts about stuff", "summary": "", "category": None})
    assert TopicScorer(cfg).total(h) > TopicScorer(cfg).total(weak)


def test_selector_picks_best_rejects_dups_blocked_and_explains(cfg, repo):
    add(repo, "Why a black hole swallowed a star", status="published")
    a = add(repo, "Why a black hole swallowed a star last year", summary="same story")
    b = add(repo, "Strange unexplained black hole mystery found by Webb telescope", summary="Webb observed a puzzling "
            "black hole that astronomers cannot explain, 13 billion years old.", category="black_holes",
            source_name="NASA", signal_data={"coverage": 2, "reliability": "high"})
    c = add(repo, "Top 10 facts about stuff", summary="list")
    d = add(repo, "Doomsday planet will end the world", summary="x")
    sel = TopicSelector(cfg, repo, TopicScorer(cfg), DuplicateDetector(repo, cfg))
    res = sel.select(use_llm=False)
    assert res["selected"]["id"] == b and res["selected"]["status"] == "selected"
    assert "Score" in res["reason"] and "Research signal only" in res["reason"]
    assert repo.get("topics", a)["status"] == "rejected" and "duplicate" in repo.get("topics", a)["rejection_reason"]
    assert "blocked" in repo.get("topics", d)["rejection_reason"]
    assert repo.get("topics", c)["status"] == "rejected"
    stored = repo.get("topics", b)["score_json"]
    assert "criteria" in stored and stored["method"] == "heuristic"


def test_selector_uses_llm_scores_and_history_overrides_originality(cfg, repo):
    add(repo, "Europa ocean plumes", status="published")
    t = add(repo, "Fresh quasar jet puzzle", summary="quasar jet astronomers puzzled", category="black_holes")
    crit = {k: 9 for k in ["curiosity", "audience_interest", "visual_potential", "scientific_importance", "mystery",
                           "emotional_impact", "relevance", "originality", "available_visuals", "retention_potential"]}
    crit.update(id=t, competition=None, reason="Great hook")
    ai = manager(cfg, repo, {"f": FakeProvider("f", replies=[{"scores": [crit]}])})
    sel = TopicSelector(cfg, repo, TopicScorer(cfg, ai), DuplicateDetector(repo, cfg))
    res = sel.select(use_llm=True)
    assert res["method"] == "llm" and "Great hook" in res["reason"]


def test_performance_weights_change_ranking(cfg, repo):
    a = add(repo, "Alpha planet atmosphere strange discovery", summary="planet atmosphere strange", category="planets")
    b = add(repo, "Beta black hole strange discovery here", summary="black hole strange", category="black_holes")
    sel = TopicSelector(cfg, repo, TopicScorer(cfg), DuplicateDetector(repo, cfg))
    repo.set_setting("topic_weights", {"planets": 1.25, "black_holes": 0.8})
    ev = sel.evaluate(repo.list_topics(), use_llm=False)
    assert ev[0]["topic"]["id"] == a


# ---------------- research & fact-check ----------------
RESEARCH = {"summary": "s", "facts": [
    {"claim": "Astronomers found a black hole 13 billion years old.", "status": "confirmed", "sources": ["S1"]},
    {"claim": "It may have formed from a direct collapse of gas.", "status": "speculation", "sources": ["S1"]}],
    "excerpts": {"S1": "NASA reported a black hole 13 billion years old. It may have formed by direct collapse."},
    "open_questions": ["How did it grow so fast?"], "sources": [{"id": "S1", "url": "https://e.org", "publisher": "NASA"}],
    "warnings": []}


def test_factchecker_flags_invented_numbers_unhedged_theory_certainty_quotes(cfg):
    fc = FactChecker(cfg)
    ok = fc.check("A black hole 13 billion years old was found. It may have formed from direct collapse.", RESEARCH)
    assert ok["passed"], ok["issues"]
    types = lambda t: {i["type"] for i in fc.check(t, RESEARCH)["issues"]}
    assert "unsupported_number" in types("A black hole 99 billion years old was found.")
    assert "unhedged_uncertain_claim" in types("It formed from a direct collapse of gas.")
    assert "overstated_certainty" in types("It definitely formed from direct collapse of gas.")
    assert "unsourced_attribution" in types("ESA announced a new mission to it.")
    assert "unverified_quote" in types('A scientist said "this changes everything we know about space".')


def test_research_extractive_fallback_is_verbatim_and_flagged(cfg, repo):
    t = {"id": 1, "title": "Black hole puzzle", "summary": "", "url": "https://e.org/a", "source_name": "NASA"}
    page = ("A distant black hole has been measured by astronomers with the Webb telescope this year. "
            "Researchers say this black hole may have grown unusually fast in the early universe. Subscribe to our newsletter today for more.")
    page = page + " "
    eng = ResearchEngine(cfg, repo, ai=None, fetcher=lambda u: page * 2)
    cfg.set("research.wikipedia_enabled", False)
    eng._wikipedia = lambda topic: None
    t["id"] = add(repo, "Black hole puzzle")
    vid = repo.create_video(t["id"], "x", "REVIEW")
    rec = eng.research(t, vid)
    assert rec["method"] == "extractive" and rec["facts"]
    assert all(f["claim"] in page * 2 for f in rec["facts"])               # verbatim only
    assert not any("newsletter" in f["claim"].lower() for f in rec["facts"])
    assert any(f["status"] == "speculation" for f in rec["facts"])
    assert rec["warnings"] and repo.get_research(vid)["method"] == "extractive"


def test_research_drops_unsourced_llm_facts(cfg, repo):
    reply = {"summary": "s", "facts": [{"claim": "Real one", "status": "confirmed", "sources": ["S1"]},
                                       {"claim": "Invented", "status": "confirmed", "sources": ["S9"]}]}
    ai = manager(cfg, repo, {"f": FakeProvider("f", replies=[reply])})
    eng = ResearchEngine(cfg, repo, ai=ai, fetcher=lambda u: "x " * 200)
    eng._wikipedia = lambda t: None
    tid = add(repo, "T topic here")
    vid = repo.create_video(tid, "x", "REVIEW")
    rec = eng.research({"id": tid, "title": "T topic here", "url": "https://e.org/a", "summary": ""}, vid)
    assert [f["claim"] for f in rec["facts"]] == ["Real one"]


# ---------------- scripts ----------------
GOOD = [
    ("hook", "Somewhere far away, a black hole should not exist."),
    ("question", "How did it grow so large so early?"),
    ("story", "Astronomers found a black hole 13 billion years old. Its mass is far beyond what models expect for such a young universe. "
              "Scientists measured it with careful observations across many nights of data."),
    ("escalation", "It may have formed from a direct collapse of gas. That would mean the early universe built giants almost instantly. "
                   "Each new measurement makes the puzzle deeper."),
    ("payoff", "If that idea holds, black holes could have started enormous, skipping the slow growth we always assumed. "
               "Nobody knows yet whether that is true or whether our models are missing something basic."),
    ("ending", "For now, the oldest giants keep their secrets, and the universe keeps asking the questions. "
               "Somewhere out there, the answer is waiting in light that left long before us."),
]


def sections(items=GOOD):
    return [{"section": s, "text": t, "visual": "black hole", "sfx": "none"} for s, t in items]


def test_validator_accepts_good_and_rejects_bad(cfg):
    v = ContentValidator(cfg)
    res = v.validate(sections())
    assert res["passed"], res["issues"]
    assert 28 <= res["duration"] <= 70
    bad = sections()
    bad[0]["text"] = "Hello everyone, welcome to our channel where we talk about a very very long hook that keeps going on and on indeed today."
    types = {i["type"] for i in v.validate(bad)["issues"]}
    assert {"banned_phrase", "generic_intro", "hook_too_long"} <= types
    assert "structure" in {i["type"] for i in v.validate(sections()[:4])["issues"]}
    short = [{"section": s, "text": "Short.", "visual": "", "sfx": "none"} for s, _ in GOOD]
    assert "too_short" in {i["type"] for i in v.validate(short)["issues"]}
    assert estimate_duration("word " * 150) > 60


def test_script_llm_retries_with_feedback_until_valid(cfg, repo):
    bad = {"sections": sections(GOOD[:5] + [("ending", "It definitely formed from direct collapse of 99 stars. Subscribe now.")])}
    good = {"sections": sections(), "pronunciation": [], "uncertainty_notes": "formation unknown"}
    prov = FakeProvider("f", replies=[bad, good])
    ai = manager(cfg, repo, {"f": prov})
    sw = ScriptWriter(cfg, ai, ContentValidator(cfg), FactChecker(cfg, None))
    out = sw.write({"id": 1, "title": "Black hole puzzle"}, RESEARCH)
    assert out["generated_by"] == "llm" and prov.calls == 2
    assert out["validation"]["passed"] and out["factcheck"]["passed"]
    assert len(out["sections"]) == 6 and out["captions"]


def test_script_fails_after_max_attempts_instead_of_lying(cfg, repo):
    bad = {"sections": sections(GOOD[:5] + [("ending", "It definitely formed from direct collapse of 99 stars.")])}
    ai = manager(cfg, repo, {"f": FakeProvider("f", replies=[bad, bad, bad])})
    sw = ScriptWriter(cfg, ai, ContentValidator(cfg), FactChecker(cfg, None))
    with pytest.raises(ValidationFailed):
        sw.write({"id": 1, "title": "x"}, RESEARCH)


def test_script_extractive_fallback_when_no_llm(cfg):
    research = {"summary": "s", "sources": [], "open_questions": ["How did it grow?"], "excerpts": {},
                "facts": [{"claim": f"Astronomers measured feature number {i} of the distant black hole in detail today.",
                           "status": "confirmed", "sources": ["S1"]} for i in range(5)]}
    sw = ScriptWriter(cfg, None, ContentValidator(cfg), FactChecker(cfg))
    out = sw.write({"id": 1, "title": "Distant black hole measured"}, research)
    assert out["generated_by"] == "extractive" and "review" in out["uncertainty_notes"].lower()
    assert [s["section"] for s in out["sections"]] == ["hook", "question", "story", "escalation", "payoff", "ending"]
    cfg.set("ai.allow_extractive_fallback", False)
    with pytest.raises(ValidationFailed):
        ScriptWriter(cfg, None, ContentValidator(cfg), FactChecker(cfg)).write({"id": 1, "title": "x"}, research)


# ---------------- scenes ----------------
def script_pkg():
    return {"sections": sections(), "narration": " ".join(t for _, t in GOOD)}


def test_scene_planner_matches_visuals_to_narration_and_preserves_text(cfg):
    sp = ScenePlanner(cfg)
    topic = {"title": "Black hole puzzle", "summary": ""}
    pkg = script_pkg()
    pkg["sections"][2]["text"] = "A rocky planet orbits close to its star. Its atmosphere glows."
    pkg["sections"][2]["visual"] = ""
    pkg["narration"] = " ".join(s["text"] for s in pkg["sections"])
    scenes = sp.plan(pkg, topic)
    assert 5 <= len(scenes) <= 8
    assert sp.validate(scenes, pkg["narration"]) == []
    planet = next(s for s in scenes if "rocky planet" in s["narration"])
    assert planet["visual_kind"] in ("planet", "orbit")
    required = {"idx", "narration", "est_duration", "visual_description", "visual_prompt", "camera", "lighting",
                "atmosphere", "sfx", "transition", "visual_kind", "search_query"}
    assert all(required <= set(s) and s["est_duration"] > 0 for s in scenes)
    assert [s["idx"] for s in scenes] == list(range(len(scenes)))


def test_scene_planner_rejects_incomplete_llm_plan_and_falls_back(cfg, repo):
    ai = manager(cfg, repo, {"f": FakeProvider("f", replies=[{"scenes": [{"sentence_indices": [0, 1]}]}])})
    scenes = ScenePlanner(cfg, ai).plan(script_pkg(), {"title": "t", "summary": ""})
    assert len(scenes) >= 5          # heuristic took over


# ---------------- SEO ----------------
def test_seo_honest_titles_limited_hashtags_and_sources(cfg):
    seo = SEOGenerator(cfg, None)
    topic = {"title": "Black hole puzzle in the early universe", "category": "black_holes"}
    meta = seo.generate(topic, script_pkg(), RESEARCH)
    assert seo.validate(meta) == []
    assert len(meta["hashtags"]) <= 3 and meta["hashtags"][-1] == "#Shorts"
    assert "https://e.org" in meta["description"] and "AI-generated" in meta["description"]
    assert meta["category_id"] == "28" and meta["tags"] and meta["thumbnail_text"]
    assert seo.title_issues("YOU WON'T BELIEVE THIS SHOCKING BLACK HOLE!!") != []
    assert seo.score_title("NASA is hiding this black hole", "x") == -1.0


def test_seo_llm_titles_filtered_and_best_chosen(cfg, repo):
    reply = {"titles": ["SHOCKING black hole you won't believe", "Why does this black hole exist at all?",
                        "BH"], "thumbnail_text": "IMPOSSIBLE BLACK HOLE", "keywords": ["black hole", "casino", "early universe"],
             "hashtags": ["#BlackHole", "#Space", "#Astronomy", "#Cosmos"], "summary": "A black hole too big for its age."}
    ai = manager(cfg, repo, {"f": FakeProvider("f", replies=[reply])})
    meta = SEOGenerator(cfg, ai).generate({"title": "t", "category": "black_holes"}, script_pkg(), RESEARCH)
    assert meta["title"] == "Why does this black hole exist at all?"
    assert "casino" not in meta["keywords"]                 # unrelated keyword removed
    assert len(meta["hashtags"]) == 3
