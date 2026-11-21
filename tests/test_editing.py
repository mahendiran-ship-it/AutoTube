"""Phase 5: subtitles, audio mixer, video editor, thumbnails."""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from app.core.resource_monitor import ResourceMonitor
from app.editing.audio_mixer import AudioMixer, MusicLibrary, synth_ambient, synth_sfx
from app.editing.subtitles import (Caption, build_captions, validate_captions, write_ass, write_srt)
from app.editing.thumbnail_generator import ThumbnailGenerator, frame_score
from app.editing.video_editor import VideoEditor, clip_lengths
from app.generation.voice_generator import VoiceGenerator
from app.utils import ffmpeg as ff
from tests.conftest import needs_ffmpeg

TIMINGS = [{"idx": 0, "start": 0.15, "end": 4.0, "sentences": [
    {"text": "Somewhere far away, a black hole should not exist.", "start": 0.15, "end": 3.0},
    {"text": "How did it grow?", "start": 3.2, "end": 4.0}]},
    {"idx": 1, "start": 4.0, "end": 8.0, "sentences": [{"text": "Nobody knows yet whether that is true or not at all.", "start": 4.0, "end": 7.5}]}]


# ---------------- subtitles ----------------
def test_captions_are_short_ordered_and_inside_sentence_bounds(cfg):
    caps = build_captions(TIMINGS, cfg)
    assert caps and all(len(c.text.split()) <= 4 for c in caps)
    assert all(a.end <= b.start + 1e-6 for a, b in zip(caps, caps[1:]))
    assert caps[0].start == pytest.approx(0.15) and caps[-1].end <= 7.5 + 1e-6
    assert caps[0].text.isupper()
    narration = " ".join(s["text"] for t in TIMINGS for s in t["sentences"])
    assert validate_captions(caps, 8.0, cfg, narration)["ok"]


def test_caption_validation_catches_overlap_and_missing_words(cfg):
    bad = [Caption(0, 2, "ONE TWO"), Caption(1.5, 3, "THREE")]
    res = validate_captions(bad, 3, cfg, "one two three four five six seven eight nine ten")
    assert not res["ok"] and any("overlap" in p for p in res["problems"]) and any("cover" in p for p in res["problems"])


def test_srt_and_ass_files(cfg, tmp_path):
    caps = build_captions(TIMINGS, cfg)
    srt = write_srt(caps, tmp_path / "c.srt", cfg).read_text()
    assert srt.startswith("1\n00:00:00,150 -->") and "\n\n" in srt
    ass = write_ass(caps, tmp_path / "c.ass", 1080, 1920, cfg).read_text()
    assert "PlayResY: 1920" in ass and ",2,97,97,614,1" in ass       # bottom-centre, 32% bottom margin = Shorts-UI-safe
    assert ass.count("Dialogue:") == len(caps) and "{\\fad" in ass


def test_ass_escapes_braces(cfg, tmp_path):
    ass = write_ass([Caption(0, 1, "A {\\b1}HACK")], tmp_path / "x.ass", 720, 1280, cfg).read_text()
    assert "{\\b1}" not in ass.split("Dialogue:")[1].split("}", 1)[1]


# ---------------- audio ----------------
def test_synth_sfx_are_finite_and_bounded():
    for n in ("low_boom", "whoosh", "shimmer", "riser", "heartbeat"):
        x = synth_sfx(n)
        assert np.isfinite(x).all() and 0.02 < np.abs(x).max() <= 1.5 and len(x) > 4800
    assert len(synth_ambient(3.0)) == 3 * 48000


def test_music_library_requires_license_and_file(cfg, tmp_path):
    d = cfg.path("assets_dir", "music")
    (d / "a.mp3").write_bytes(b"x")
    (d / "b.mp3").write_bytes(b"x")
    (d / "library.yaml").write_text(
        "tracks:\n - {file: a.mp3, title: A, license: 'CC0'}\n - {file: b.mp3, title: B}\n - {file: missing.mp3, title: C, license: 'CC0'}\n"
        " - {file: '../evil.mp3', title: D, license: 'CC0'}\n")
    t = MusicLibrary(cfg).tracks()
    assert [x["title"] for x in t] == ["A"]


@needs_ffmpeg
def test_mix_without_music_is_audible_unclipped_and_right_length(cfg, tmp_path):
    cfg.set("audio.music.enabled", True)       # library empty → must still work
    vg = VoiceGenerator(cfg).synthesize([{"idx": 0, "narration": "One short sentence here."},
                                         {"idx": 1, "narration": "Another short sentence there."}], tmp_path / "v")
    scenes = [{"idx": 0, "sfx": "low_boom"}, {"idx": 1, "sfx": "riser"}]
    res = AudioMixer(cfg).mix(vg["path"], vg["duration"], vg["scenes"], scenes, tmp_path / "a")
    assert res["music"] is None and abs(res["duration"] - vg["duration"]) < 0.15
    assert res["max_db"] <= -0.9 and res["mean_db"] > -40
    assert len(res["effects"]) == 3             # ambient + 2 sfx


@needs_ffmpeg
def test_mix_with_music_ducks_under_narration_and_credits(cfg, tmp_path):
    d = cfg.path("assets_dir", "music")
    ff.run(["-f", "lavfi", "-i", "sine=frequency=330:duration=6", "-ar", "44100", str(d / "t.wav")])
    (d / "library.yaml").write_text("tracks:\n - {file: t.wav, title: T, author: Me, license: 'CC BY 4.0', attribution_required: true}\n")
    vg = VoiceGenerator(cfg).synthesize([{"idx": 0, "narration": "Narration over the music bed."}], tmp_path / "v")
    res = AudioMixer(cfg).mix(vg["path"], vg["duration"], vg["scenes"], [{"idx": 0, "sfx": "none"}], tmp_path / "a")
    assert res["music"]["title"] == "T" and "Music: T by Me" in res["credits"][0]
    assert res["max_db"] <= -0.9


# ---------------- clip lengths & editor ----------------
def test_clip_lengths_keep_crossfades_centered_on_boundaries():
    T = 0.4
    ends = [3.0, 7.0, 10.0]
    lens = clip_lengths(ends, 10.0, T)
    acc = lens[0]
    for i in range(1, len(lens)):
        offset = acc - T
        assert offset + T / 2 == pytest.approx(ends[i - 1])        # fade is centred on the narration boundary
        acc = offset + lens[i]
    assert acc == pytest.approx(10.0)


def make_clips(tmp_path, n, w, h):
    clips = []
    for i, col in enumerate(["red", "green", "blue", "yellow"][:n]):
        p = tmp_path / f"c{i}.mp4"
        ff.run(["-f", "lavfi", "-i", f"color=c={col}:s={w}x{h}:r=24:d=6,noise=alls=40:allf=t", "-pix_fmt", "yuv420p", "-c:v", "libx264",
                "-preset", "ultrafast", str(p)])
        clips.append(p)
    return clips


@needs_ffmpeg
def test_editor_assembles_synced_vertical_mp4_with_subtitles(cfg, tmp_path):
    cfg.set("video.fps", 24)
    cfg.set("video.presets.low", {"width": 144, "height": 256, "crf": 30, "x264_preset": "ultrafast"})
    cfg.set("video.preset", "low")
    vg = VoiceGenerator(cfg).synthesize([{"idx": 0, "narration": "First scene sentence here."},
                                         {"idx": 1, "narration": "Second scene sentence now."},
                                         {"idx": 2, "narration": "Third and final scene."}], tmp_path / "v")
    total = vg["duration"]
    ends = [s["end"] for s in vg["scenes"]]
    T = float(cfg.get("video.transition_seconds"))
    lens = clip_lengths(ends, total, T)
    clips = []
    for i, ln in enumerate(lens):
        p = tmp_path / f"s{i}.mp4"
        ff.run(["-f", "lavfi", "-i", f"testsrc2=size=144x256:rate=24:duration={ln}", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", str(p)])
        clips.append(p)
    caps = build_captions(vg["scenes"], cfg)
    ass = write_ass(caps, tmp_path / "caps.ass", 144, 256, cfg)
    audio = AudioMixer(cfg).mix(vg["path"], total, vg["scenes"], [{"idx": i, "sfx": "none"} for i in range(3)], tmp_path / "a")
    ed = VideoEditor(cfg, ResourceMonitor(cfg))
    out = tmp_path / "final.mp4"
    info = ed.assemble(clips, ends, ["fade", "dissolve"], audio["path"], ass, out, total)
    p = ff.probe(out)
    assert (p["width"], p["height"]) == (144, 256) and p["video_codec"] == "h264" and p["audio_codec"] == "aac"
    assert abs(p["duration"] - total) < 0.15                       # crossfades did not drift the timeline
    assert p["pix_fmt"] == "yuv420p" and info["encoder"] == "libx264"


@needs_ffmpeg
def test_editor_survives_paths_with_spaces_and_rejects_unknown_transition(cfg, tmp_path):
    cfg.set("video.preset", "low")
    cfg.set("video.presets.low", {"width": 144, "height": 256, "crf": 30, "x264_preset": "ultrafast"})
    d = tmp_path / "my folder"
    d.mkdir()
    clips = make_clips(d, 2, 144, 256)
    audio = d / "a.wav"
    ff.run(["-f", "lavfi", "-i", "sine=frequency=200:duration=5", "-ac", "2", str(audio)])
    out = VideoEditor(cfg, ResourceMonitor(cfg)).assemble(clips, [2.5, 5.0], ["evil;transition"], audio, None, d / "o.mp4", 5.0)
    assert abs(out["duration"] - 5.0) < 0.15       # unknown transition → safe 'fade'


# ---------------- thumbnails ----------------
def test_frame_score_prefers_rich_frames_over_blank_and_dark():
    rng = np.random.default_rng(0)
    rich = Image.fromarray((rng.random((170, 96, 3)) * 255).astype(np.uint8))
    dark = Image.new("RGB", (96, 170), (3, 3, 5))
    flat = Image.new("RGB", (96, 170), (120, 120, 120))
    assert frame_score(rich) > 20 and frame_score(dark) == 0 and frame_score(flat) == 0


def test_thumbnail_text_is_sanitised(cfg):
    g = ThumbnailGenerator(cfg)
    assert g.sanitize_text("<b>why is this black hole impossible to explain</b>") == "WHY IS THIS BLACK HOLE"
    assert "SHOCKING" not in g.sanitize_text("shocking black hole")


@needs_ffmpeg
def test_thumbnail_created_with_text_and_size_limit(cfg, tmp_path):
    clip = tmp_path / "c.mp4"
    ff.run(["-f", "lavfi", "-i", "testsrc2=size=270x480:rate=10:duration=2", "-pix_fmt", "yuv420p", str(clip)])
    dark = tmp_path / "dark.mp4"
    ff.run(["-f", "lavfi", "-i", "color=c=black:s=270x480:r=10:d=2", "-pix_fmt", "yuv420p", str(dark)])
    res = ThumbnailGenerator(cfg).create([dark, clip], "Impossible Black Hole", tmp_path / "t.jpg")
    assert res["frame"]["source"].endswith("c.mp4")                  # blank frame rejected
    im = Image.open(res["path"])
    assert im.size == (1080, 1920) and res["bytes"] < 2_000_000 and res["phrase"] == "IMPOSSIBLE BLACK HOLE"
    wide = ThumbnailGenerator(cfg).create([clip], "Why", tmp_path / "w.jpg", long_form=True)
    assert Image.open(wide["path"]).size == (1280, 720)
