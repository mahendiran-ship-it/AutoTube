"""Quality control: every video is inspected before it may be uploaded.

Each check has a severity. ANY failed `critical` check blocks upload. Scores (0-10) are computed from measurements,
not from the model's opinion.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

from app.core.logger import get_logger
from app.utils import ffmpeg as ff

log = get_logger("qc")


class QCEngine:
    def __init__(self, cfg, repo):
        self.cfg, self.repo = cfg, repo

    def _c(self, key, default):
        return self.cfg.get(f"qc.{key}", default)

    # ------------------------------------------------------------------
    def inspect(self, video_id: int) -> dict:
        v = self.repo.get_video(video_id)
        checks: list[dict] = []

        def add(name, ok, severity="critical", detail=""):
            checks.append({"name": name, "ok": bool(ok), "severity": severity, "detail": str(detail)[:300]})
            return ok

        scores: dict[str, float] = {}
        info = None
        path = Path(v["final_path"]) if v and v.get("final_path") else None
        if not add("video_file_exists", path and path.is_file(), detail=str(path)):
            return self._finish(video_id, checks, scores, v)
        try:
            info = ff.probe(path)
            add("video_not_corrupted", True)
        except Exception as e:
            add("video_not_corrupted", False, detail=e)
            return self._finish(video_id, checks, scores, v)

        add("format_supported", info["format"] and "mp4" in info["format"] and info["video_codec"] == "h264"
            and info["audio_codec"] == "aac" and info["pix_fmt"] == "yuv420p",
            detail=f"{info['format']}/{info['video_codec']}/{info['audio_codec']}/{info['pix_fmt']}")
        dmin, dmax = float(self._c("duration_min_seconds", 28)), float(self._c("duration_max_seconds", 60))
        add("duration_in_range", dmin <= info["duration"] <= dmax, detail=f"{info['duration']:.1f}s (allowed {dmin}-{dmax}s)")
        ratio = (info["width"] or 1) / (info["height"] or 1)
        add("aspect_ratio_9_16", abs(ratio - float(self._c("aspect_ratio", 0.5625))) <= float(self._c("aspect_tolerance", 0.02)),
            detail=f"{info['width']}x{info['height']}")
        add("has_video_and_audio_streams", info["has_video"] and info["has_audio"])

        # ---- audio -------------------------------------------------------
        audio_score = 0.0
        if info["has_audio"]:
            vol = ff.volumedetect(path)
            floor = float(self._c("min_narration_mean_db", -38))
            audible = vol["mean_db"] is not None and vol["mean_db"] > floor
            add("audio_audible", audible, detail=f"mean {vol['mean_db']} dB (floor {floor})")
            clip_ok = vol["max_db"] is None or vol["max_db"] <= float(self._c("max_clipping_peak_db", -0.1))
            add("audio_not_clipping", clip_ok, "critical", f"peak {vol['max_db']} dB")
            audio_score = 10.0 if audible else 0.0
            if vol["mean_db"] is not None and audible:
                audio_score = max(2.0, 10 - abs(vol["mean_db"] - (-16)) * 0.35)
            if not clip_ok:
                audio_score = min(audio_score, 4.0)
        narr_path = self._narration_path(video_id)
        narration_score = 0.0
        if narr_path and narr_path.is_file():
            sil = ff.silencedetect(narr_path, float(self._c("silence_noise_db", -45)), float(self._c("max_silence_seconds", 2.5)))
            add("no_long_silence_in_narration", not sil, detail=f"{len(sil)} silent gap(s) > {self._c('max_silence_seconds', 2.5)}s")
            nv = ff.volumedetect(narr_path)
            narration_ok = nv["mean_db"] is not None and nv["mean_db"] > float(self._c("min_narration_mean_db", -38))
            add("narration_audible", narration_ok, detail=f"narration mean {nv['mean_db']} dB")
            narration_score = 9.0 if narration_ok and not sil else (5.0 if narration_ok else 0.0)
        else:
            add("narration_audible", False, detail="narration file missing")
        render = (v.get("render_info_json") or {}) if v else {}
        placeholder = bool(render.get("narration_placeholder"))
        add("narration_is_real_speech", not placeholder, "critical",
            "narration came from the placeholder test voice" if placeholder else "")
        if placeholder:
            narration_score = 0.0

        # ---- visuals -----------------------------------------------------
        scenes = self.repo.list_scenes(video_id)
        assets = {a["id"]: a for a in self.repo.list_assets(video_id)}
        def clip_ok(sc: dict) -> bool:
            a = assets.get(sc.get("asset_id"))
            return bool(a and a.get("path") and Path(a["path"]).is_file())

        missing = [sc["idx"] for sc in scenes if not clip_ok(sc)]
        add("visual_scenes_exist", bool(scenes) and not missing, detail=f"scenes={len(scenes)} missing={missing}")
        black_ratio, frozen = self._visual_stats(path, info["duration"])
        add("no_blank_frames", black_ratio <= float(self._c("max_black_ratio", 0.04)), "critical", f"black {black_ratio:.1%}")
        add("frames_have_detail", frozen == 0, "critical", f"{frozen} flat sampled frame(s)")
        visual_score = 10.0
        visual_score -= min(6.0, black_ratio * 100) + frozen * 2
        visual_score -= 0.0 if (info["height"] or 0) >= 1280 else 1.0
        methods = [s.get("visual_method") for s in scenes]
        if methods and not all(methods):
            visual_score -= 2
        visual_score = max(0.0, visual_score)

        # ---- subtitles / thumbnail / metadata -----------------------------
        srt = Path(v["srt_path"]) if v.get("srt_path") else None
        add("subtitles_exist", srt and srt.is_file() and srt.stat().st_size > 20, "critical", str(srt))
        if self._c("require_burned_subtitles", True):
            add("subtitles_burned_in", bool((render.get("subtitles_burned"))), "warning",
                "subtitles are not burned into the video" if not render.get("subtitles_burned") else "")
        subtitle_score = 9.0 if srt and srt.is_file() else 0.0
        if render.get("caption_validation") and not render["caption_validation"].get("ok", True):
            subtitle_score = 5.0
            add("subtitle_quality", False, "warning", "; ".join(render["caption_validation"].get("problems", []))[:200])
        thumb = Path(v["thumbnail_path"]) if v.get("thumbnail_path") else None
        thumb_ok = False
        if thumb and thumb.is_file():
            try:
                with Image.open(thumb) as im:
                    thumb_ok = im.size[0] > 200 and thumb.stat().st_size < 2_000_000
            except Exception:
                thumb_ok = False
        add("thumbnail_valid", thumb_ok, "critical", str(thumb))
        meta = v.get("metadata_json") or {}
        meta_ok = bool(meta.get("title") and meta.get("description") and meta.get("tags"))
        add("metadata_exists", meta_ok, "critical")
        if meta_ok:
            probs = self._meta_problems(meta)
            add("metadata_honest_and_valid", not probs, "critical", "; ".join(probs))

        # ---- content ------------------------------------------------------
        script = self.repo.get_script(video_id)
        if script:
            add("script_validated", bool((script.get("validation_json") or {}).get("passed")), "critical")
            fc = script.get("factcheck_json") or {}
            add("fact_check_passed", bool(fc.get("passed")), "critical", f"{len([i for i in fc.get('issues', []) if i.get('severity') == 'error'])} issue(s)")
            add("script_written_by_ai_writer", script.get("generated_by") != "extractive", "warning",
                "script was assembled from source sentences (no LLM available); review manually")
        else:
            add("script_validated", False)
        research = self.repo.get_research(video_id)
        add("research_record_exists", bool(research and research.get("sources_json")), "critical")

        # ---- licenses & duplicates ------------------------------------------
        if self._c("require_license_records", True):
            bad = [a["id"] for a in assets.values()
                   if a["method"] in ("stock_video", "stock_image", "music") and not a.get("license_id")]
            ungen = [a for a in assets.values() if a["method"] in ("stock_video", "stock_image", "music")]
            add("source_and_license_records", not bad, "critical", f"{len(ungen)} third-party asset(s), {len(bad)} without license")
        h = ff.sha256_file(path)
        dup = self.repo.video_by_hash(h, exclude_id=video_id)
        add("not_a_repeat_video", dup is None, "critical", f"same file as video #{dup['id']}" if dup else "")
        self.repo.update_video(video_id, content_hash=h)
        topic = self.repo.get("topics", v["topic_id"]) if v.get("topic_id") else None
        topic_score = float((topic or {}).get("score_total") or 0)
        scores = {"visual": round(visual_score, 1), "audio": round(audio_score, 1), "narration": round(narration_score, 1),
                  "subtitles": round(subtitle_score, 1), "topic": round(min(10.0, topic_score), 1)}
        return self._finish(video_id, checks, scores, v, info)

    # ------------------------------------------------------------------
    def _narration_path(self, video_id: int) -> Path | None:
        for a in self.repo.list_assets(video_id):
            if a["method"] == "tts":
                return Path(a["path"])
        return None

    def _meta_problems(self, meta: dict) -> list[str]:
        out = []
        t = meta.get("title", "")
        low = t.lower()
        for p in self.cfg.get("seo.banned_title_phrases") or []:
            if p.lower() in low:
                out.append(f"banned phrase in title: {p}")
        if len(t) > int(self.cfg.get("seo.title_max_chars", 90)) or len(t) < 8:
            out.append("title length invalid")
        if len(meta.get("hashtags", [])) > int(self.cfg.get("seo.max_hashtags", 3)):
            out.append("too many hashtags")
        if len(meta.get("description", "")) > 5000:
            out.append("description too long")
        return out

    def _visual_stats(self, path: Path, duration: float) -> tuple[float, int]:
        black = sum(b["duration"] for b in ff.blackdetect(path))
        flat = 0
        with tempfile.TemporaryDirectory() as d:
            for k in range(1, 9):
                t = duration * k / 9
                try:
                    p = ff.extract_frame(path, t, Path(d) / f"f{k}.jpg")
                    a = np.asarray(Image.open(p).convert("L"), np.float32)
                    if a.std() < 3.0:
                        flat += 1
                except Exception:
                    flat += 1
        return black / max(duration, 0.1), flat

    def _finish(self, video_id, checks, scores, v, info=None) -> dict:
        critical = [c for c in checks if c["severity"] == "critical" and not c["ok"]]
        warnings = [c for c in checks if c["severity"] != "critical" and not c["ok"]]
        weights = {"visual": 1.2, "audio": 1.0, "narration": 1.2, "subtitles": 0.8, "topic": 0.8}
        num = sum(scores.get(k, 0) * w for k, w in weights.items())
        total = round(num / sum(weights.values()), 2) if scores else 0.0
        floor = float(self._c("min_total_score", 6.5))
        passed = not critical and total >= floor
        reasons = [f"{c['name']}: {c['detail']}" for c in critical]
        if not critical and total < floor:
            reasons.append(f"quality score {total} below minimum {floor}")
        report = {"passed": passed, "total": total, "scores": scores, "critical_failures": reasons,
                  "warnings": [f"{w['name']}: {w['detail']}" for w in warnings], "checks": checks,
                  "needs_human_review": bool(warnings)}
        try:
            self.repo.update_video(video_id, qc_json=report, quality_score=total,
                                   status="qc_passed" if passed else "qc_failed")
        except Exception as e:  # pragma: no cover
            log.error("could not store QC report: %s", e)
        log.info("QC video %s: %s (score %s, %d critical)", video_id, "PASSED" if passed else "FAILED", total, len(critical))
        return report
