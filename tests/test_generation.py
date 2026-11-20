"""Phase 4: voice, procedural visuals, image motion, stock providers, visual fallback chain."""
from __future__ import annotations

import base64
import io
import json
import sys

import numpy as np
import pytest
from PIL import Image

from app.core.budget import BudgetController
from app.core.errors import ConfigurationError, ProviderUnavailable, UnsafeURL
from app.generation import stock_provider as sp
from app.generation.image_motion import image_to_clip
from app.generation.procedural_visuals import KINDS, TimelineScene, render_procedural
from app.generation.video_provider import CommandVideoProvider
from app.generation.visual_kinds import detect_visual_kind
from app.generation.visual_manager import VisualManager, normalize_clip
from app.generation.voice_generator import (TestToneTTS, TTSAdapter, VoiceGenerator, apply_pronunciations, read_wav)
from app.utils import ffmpeg as ff
from tests.conftest import needs_ffmpeg

pytestmark = needs_ffmpeg


# ---------------- voice ----------------
def test_pronunciation_replacement_is_whole_word_and_longest_first():
    t = apply_pronunciations("Betelgeuse and JWST, not JWSTX.", {"Betelgeuse": "Betel juice", "JWST": "Webb"})
    assert t == "Betel juice and Webb, not JWSTX."


def scenes_for_voice():
    return [{"idx": 0, "narration": "First sentence here. Second one follows."}, {"idx": 1, "narration": "A final scene sentence."}]


def test_voice_synthesis_timing_and_validation(cfg, tmp_path):
    vg = VoiceGenerator(cfg)                  # conftest config orders: test_tone
    res = vg.synthesize(scenes_for_voice(), tmp_path)
    assert res["placeholder"] is True and res["provider"] == "test_tone"
    assert res["validation"]["ok"], res["validation"]
    s0, s1 = res["scenes"]
    assert s0["start"] < s0["end"] <= s1["start"] + 1e-6 and s1["end"] <= res["duration"]
    assert [len(s["sentences"]) for s in res["scenes"]] == [2, 1]
    assert abs(ff.probe(res["path"])["duration"] - res["duration"]) < 0.1
    for sc in res["scenes"]:                  # sentence timings are monotonic and inside their scene
        prev = sc["start"]
        for st in sc["sentences"]:
            assert prev <= st["start"] < st["end"] <= sc["end"] + 1e-6
            prev = st["end"]
    assert read_wav(res["path"]).max() < 0.9


def test_no_voice_available_gives_actionable_error(cfg, tmp_path):
    cfg.set("voice.provider_order", ["piper"])
    cfg.set("voice.piper.model", "data/voices/missing.onnx")
    with pytest.raises(ConfigurationError, match="Voice setup"):
        VoiceGenerator(cfg).synthesize(scenes_for_voice(), tmp_path)


def test_voice_falls_through_to_next_adapter(cfg, tmp_path):
    class Down(TTSAdapter):
        name = "down"

        def available(self):
            return False, "nope"

        def synth(self, text, out):
            raise ProviderUnavailable("x")
    res = VoiceGenerator(cfg, adapters=[Down(), TestToneTTS()]).synthesize(scenes_for_voice(), tmp_path)
    assert res["provider"] == "test_tone"


def test_silent_narration_fails_validation(cfg, tmp_path):
    from app.generation.voice_generator import write_wav
    write_wav(tmp_path / "s.wav", np.zeros(48000, np.float32))
    v = VoiceGenerator(cfg).validate(tmp_path / "s.wav")
    assert not v["ok"] and any("quiet" in p for p in v["problems"])


# ---------------- procedural + motion ----------------
@pytest.mark.parametrize("kind", KINDS)
def test_every_procedural_kind_renders_valid_video(kind, tmp_path):
    out = tmp_path / f"{kind}.mp4"
    render_procedural(kind, out, 0.5, 144, 256, 24, seed=3, text="The universe began 13.8 billion years ago and today we look back")
    info = ff.probe(out)
    assert info["has_video"] and (info["width"], info["height"]) == (144, 256) and 0.4 < info["duration"] < 0.7
    f = ff.extract_frame(out, 0.2, tmp_path / "f.png")
    arr = np.asarray(Image.open(f).convert("L"))
    assert arr.std() > 2          # not a flat/blank frame


def test_procedural_is_deterministic_per_seed(tmp_path):
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    for p in (a, b):
        render_procedural("galaxy", p, 0.3, 96, 160, 20, seed=42)
    fa, fb = ff.extract_frame(a, 0.1, tmp_path / "fa.png"), ff.extract_frame(b, 0.1, tmp_path / "fb.png")
    assert np.array_equal(np.asarray(Image.open(fa)), np.asarray(Image.open(fb)))


def test_timeline_labels_come_only_from_narration():
    assert TimelineScene.labels_from("It formed 13.8 billion years ago, long before today.") == ["13.8 billion years ago", "today"]
    assert TimelineScene.labels_from("Nothing temporal here") == []


def test_detect_visual_kind():
    assert detect_visual_kind("light cannot escape the event horizon") == "black_hole"
    assert detect_visual_kind("a rocky planet with a thin atmosphere") == "planet"
    assert detect_visual_kind("silence") == "starfield"


def make_photo(path, w=400, h=300):
    arr = (np.random.default_rng(1).random((h, w, 3)) * 255).astype(np.uint8)
    Image.fromarray(arr).save(path)
    return path


def test_image_to_clip_makes_vertical_video(tmp_path):
    img = make_photo(tmp_path / "p.jpg")
    out = tmp_path / "m.mp4"
    image_to_clip(img, out, 0.6, 144, 256, 20, seed=1)
    info = ff.probe(out)
    assert (info["width"], info["height"]) == (144, 256) and info["duration"] > 0.5


def test_normalize_clip_crops_to_vertical_and_loops_short_source(tmp_path):
    src = tmp_path / "src.mp4"
    ff.run(["-f", "lavfi", "-i", "testsrc=size=320x180:rate=15:duration=1", "-pix_fmt", "yuv420p", str(src)])
    out = normalize_clip(src, tmp_path / "n.mp4", 2.5, 144, 256, 24, 28, "ultrafast")
    info = ff.probe(out)
    assert (info["width"], info["height"]) == (144, 256) and abs(info["duration"] - 2.5) < 0.2 and not info["has_audio"]


# ---------------- stock providers (mocked HTTP) ----------------
PEXELS_VIDEOS = {"videos": [{"id": 1, "url": "https://www.pexels.com/video/starry-night-sky-galaxy-12345/", "duration": 12,
                             "user": {"name": "Jane", "url": "https://www.pexels.com/@jane"},
                             "video_files": [{"link": "https://videos.pexels.com/a_hd.mp4", "width": 1080, "height": 1920, "file_type": "video/mp4"},
                                             {"link": "https://videos.pexels.com/a_uhd.mp4", "width": 2160, "height": 3840, "file_type": "video/mp4"}]},
                            {"id": 2, "url": "https://www.pexels.com/video/busy-city-street-traffic-999/", "duration": 9,
                             "user": {"name": "Bob", "url": ""}, "video_files": [{"link": "https://videos.pexels.com/c.mp4", "width": 1080, "height": 1920, "file_type": "video/mp4"}]}]}


def mock_json(monkeypatch, routes):
    def fake_http_get(url, **kw):
        for key, payload in routes.items():
            if key in url:
                return json.dumps(payload).encode(), "application/json", url
        raise ProviderUnavailable(f"HTTP 404 {url}")
    monkeypatch.setattr(sp, "http_get", fake_http_get)


def test_pexels_parses_license_and_picks_smallest_adequate_file(cfg, monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "k")
    mock_json(monkeypatch, {"/videos/search": PEXELS_VIDEOS})
    vids = sp.PexelsProvider(cfg).search_videos("galaxy")
    assert vids[0].url.endswith("a_hd.mp4") and vids[0].license_name == "Pexels License"
    assert "Jane" in vids[0].attribution_text and vids[0].license_record()["source_url"].startswith("https://www.pexels.com/video/")


def test_provider_disabled_without_key(cfg, monkeypatch):
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    assert not sp.PexelsProvider(cfg).enabled() and not sp.PixabayProvider(cfg).enabled()
    assert sp.NasaImagesProvider(cfg).enabled()


def test_relevance_filters_off_topic_results(cfg, repo, monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "k")
    mock_json(monkeypatch, {"/videos/search": PEXELS_VIDEOS})
    mgr = sp.StockManager(cfg, repo, providers=[sp.PexelsProvider(cfg)])
    found = mgr.find("video", "galaxy stars", ["galaxy"], None, 5)
    assert [c.page_url for c in found] == ["https://www.pexels.com/video/starry-night-sky-galaxy-12345/"]   # city clip dropped


def test_nasa_skips_third_party_copyrighted_items(cfg, monkeypatch):
    search = {"collection": {"items": [
        {"data": [{"nasa_id": "PIA1", "title": "Black hole art", "center": "JPL", "keywords": ["black hole"]}]},
        {"data": [{"nasa_id": "X2", "title": "Black hole third party", "secondary_creator": "Some Agency"}]}]}}
    asset = {"collection": {"items": [{"href": "http://images-assets.nasa.gov/image/PIA1/PIA1~orig.jpg"},
                                      {"href": "http://images-assets.nasa.gov/image/PIA1/PIA1~large.jpg"}]}}
    mock_json(monkeypatch, {"/search": search, "/asset/PIA1": asset})
    c = sp.NasaImagesProvider(cfg).search_images("black hole")
    assert len(c) == 1 and c[0].url == "https://images-assets.nasa.gov/image/PIA1/PIA1~large.jpg"
    assert c[0].attribution_required and "NASA" in c[0].attribution_text


def test_stock_reuse_across_videos_is_avoided(cfg, repo, monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "k")
    mock_json(monkeypatch, {"/videos/search": PEXELS_VIDEOS})
    other = repo.create_video(None, "other", "REVIEW")
    repo.add_asset(video_id=other, kind="clip", method="stock_video", source_url="https://www.pexels.com/video/starry-night-sky-galaxy-12345/")
    mgr = sp.StockManager(cfg, repo, providers=[sp.PexelsProvider(cfg)])
    assert mgr.find("video", "galaxy stars", ["galaxy"], video_id=999) == []


def test_download_rejects_non_allowlisted_host(cfg, repo, tmp_path):
    mgr = sp.StockManager(cfg, repo, providers=[])
    c = sp.Candidate(provider="x", kind="video", url="https://evil.example.com/a.mp4", page_url="p", width=1, height=1,
                     hosts=["pexels.com"])
    with pytest.raises(UnsafeURL):
        mgr.download(c, tmp_path, "a.mp4")


# ---------------- video AI adapter ----------------
def test_video_ai_command_runs_without_shell_and_blocks_injection(cfg, tmp_path):
    marker = tmp_path / "pwned"
    script = tmp_path / "gen.py"
    script.write_text("import sys,subprocess\nout=sys.argv[sys.argv.index('--out')+1]\n"
                      "subprocess.run(['ffmpeg','-y','-loglevel','error','-f','lavfi','-i','testsrc=size=160x284:rate=10:duration=2','-pix_fmt','yuv420p',out])\n")
    cfg.set("visuals.video_ai", {"enabled": True, "paid": False, "timeout_seconds": 60,
                                 "command": f'{sys.executable} {script} --prompt {{prompt}} --out {{output}}'})
    p = CommandVideoProvider(cfg)
    out = p.generate(f"a nebula; touch {marker}; $(touch {marker})", tmp_path / "o.mp4", 2, 160, 284)
    assert out.is_file() and not marker.exists()            # prompt was passed as ONE argument, never interpreted


def test_video_ai_disabled_by_default(cfg):
    assert CommandVideoProvider(cfg).enabled() is False


# ---------------- fallback chain ----------------
def small(cfg):
    cfg.set("visuals.procedural.internal_scale", 0.5)


def scene(idx=0, kind="black_hole", text="Light cannot escape the event horizon."):
    return {"idx": idx, "narration": text, "visual_kind": kind, "visual_description": "d", "visual_prompt": "p",
            "search_query": "black hole space", "camera": "slow push-in"}


def test_fallback_to_procedural_when_nothing_else_available(cfg, repo, tmp_path, monkeypatch):
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    cfg.set("visuals.stock.nasa_images.enabled", False)
    vm = VisualManager(cfg, repo)
    res = vm.produce(scene(), 1, tmp_path, 1.0, 144, 256, 20, 28, "ultrafast")
    assert res["method"] == "procedural" and ff.probe(res["path"])["duration"] > 0.9
    assert [a["method"] for a in res["attempts"]][-1] == "procedural"


def test_stock_failure_falls_back_and_records_attempts(cfg, repo, tmp_path, monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "k")
    cfg.set("visuals.stock.nasa_images.enabled", False)
    cfg.set("visuals.stock.pixabay.enabled", False)

    def boom(*a, **k):
        raise ProviderUnavailable("HTTP 429 rate limited")
    monkeypatch.setattr(sp, "http_get", boom)
    vm = VisualManager(cfg, repo)
    res = vm.produce(scene(), 1, tmp_path, 1.0, 144, 256, 20, 28, "ultrafast")
    assert res["method"] == "procedural"        # pipeline never stops because a provider failed


def test_stock_video_success_records_license(cfg, repo, tmp_path, monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "k")
    cfg.set("visuals.stock.nasa_images.enabled", False)
    cfg.set("visuals.stock.pixabay.enabled", False)
    mock_json(monkeypatch, {"/videos/search": PEXELS_VIDEOS})
    src = tmp_path / "dl.mp4"
    ff.run(["-f", "lavfi", "-i", "testsrc=size=270x480:rate=15:duration=3", "-pix_fmt", "yuv420p", str(src)])
    mgr = sp.StockManager(cfg, repo, providers=[sp.PexelsProvider(cfg)])
    mgr.download = lambda c, d, n: src
    vm = VisualManager(cfg, repo, stock=mgr)
    vid = repo.create_video(None, "v", "REVIEW")
    res = vm.produce(scene(kind="galaxy", text="A galaxy of stars."), vid, tmp_path / "o", 2.0, 144, 256, 20, 28, "ultrafast")
    assert res["method"] == "stock_video" and res["provider"] == "pexels"
    lic = repo.get_license(res["license_id"])
    assert lic["license_name"] == "Pexels License" and lic["author"] == "Jane"


def test_paid_video_ai_is_blocked_by_budget_then_procedural(cfg, repo, tmp_path, monkeypatch):
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    cfg.set("visuals.stock.nasa_images.enabled", False)
    cfg.set("visuals.video_ai", {"enabled": True, "paid": True, "command": "echo {output}"})
    vm = VisualManager(cfg, repo, budget=BudgetController(cfg, repo))
    res = vm.produce(scene(), 1, tmp_path, 1.0, 144, 256, 20, 28, "ultrafast")
    assert res["method"] == "procedural"
    assert any("budget" in a.get("detail", "") for a in res["attempts"])


def test_produce_all_stores_assets_and_methods(cfg, repo, tmp_path, monkeypatch):
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    cfg.set("visuals.stock.nasa_images.enabled", False)
    vid = repo.create_video(None, "v", "REVIEW")
    scs = [scene(0), scene(1, "galaxy", "A galaxy spins.")]
    ids = repo.replace_scenes(vid, None, [{"idx": s["idx"], "narration": s["narration"]} for s in scs])
    for s, i in zip(scs, ids):
        s["id"] = i
    vm = VisualManager(cfg, repo)
    out = vm.produce_all(vid, scs, tmp_path, [1.0, 1.0], 144, 256, 20, 28, "ultrafast")
    assert [r["method"] for r in out] == ["procedural", "procedural"]
    assert [a["method"] for a in repo.list_assets(vid)] == ["procedural", "procedural"]
    assert {s["visual_method"] for s in repo.list_scenes(vid)} == {"procedural"}
