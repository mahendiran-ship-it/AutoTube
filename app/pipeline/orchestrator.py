"""The daily pipeline. Every stage is a resumable task; finished stages are never redone.

Stage order:
 discover → analyze → select_topic → [topic_approval] → research → script → [script_approval] → scenes → voice →
 visuals → captions → audio → render → metadata → thumbnail → qc → [upload_approval] → upload

Nothing here claims success it did not achieve: failures stop the run, are recorded, and notify the user.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from app.core import notifications as N
from app.core.errors import (ConfigurationError, NonRetryableError, PipelinePaused, QualityCheckFailed, RepairRequested,
                             StageFailed, WaitingForApproval)
from app.core.logger import get_logger
from app.editing.subtitles import build_captions, validate_captions, write_ass, write_srt
from app.editing.video_editor import clip_lengths
from app.database.models import TaskStatus
from app.pipeline.services import Services
from app.utils.validators import slugify

log = get_logger("pipeline")

STAGES = ["discover", "analyze", "select_topic", "topic_approval", "research", "script", "script_approval", "scenes",
          "voice", "visuals", "captions", "audio", "render", "metadata", "thumbnail", "qc", "upload_approval", "upload"]
# stages whose outputs live on disk (verified on resume)
FILE_STAGES = {"voice": ["audio_path"], "visuals": ["clips"], "captions": ["srt", "ass"], "audio": ["mix_path"],
               "render": ["final"], "thumbnail": ["thumbnail"]}

SMOKE_SECTIONS = [
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
SMOKE_RESEARCH = {
    "summary": "Smoke-test fixture (not real research).", "method": "smoke_fixture", "warnings": ["fixture data, not real research"],
    "facts": [{"claim": "Astronomers found a black hole 13 billion years old.", "status": "confirmed", "sources": ["S1"]},
              {"claim": "It may have formed from a direct collapse of gas.", "status": "speculation", "sources": ["S1"]}],
    "excerpts": {"S1": "Astronomers found a black hole 13 billion years old. It may have formed from a direct collapse of gas."},
    "open_questions": ["How did it grow so fast?"], "key_terms": ["black hole"],
    "sources": [{"id": "S1", "title": "Smoke fixture", "url": "https://example.org/smoke", "publisher": "fixture", "reliability": "low"}]}


class Pipeline:
    def __init__(self, svc: Services):
        self.s = svc
        self.cfg, self.repo, self.tm = svc.cfg, svc.repo, svc.tm

    # ------------------------------------------------------------------ paths
    def stem(self, video_id: int) -> str:
        v = self.repo.get_video(video_id)
        return f"{video_id:04d}-{slugify(v.get('title') or 'video', 40)}"

    def dirs(self, video_id: int) -> dict[str, Path]:
        stem = self.stem(video_id)
        c = self.cfg
        return {"stem": stem, "scripts": c.path("output_dir", "scripts"), "audio": c.path("output_dir", "audio", stem),
                "visuals": c.path("output_dir", "visuals", stem), "videos": c.path("output_dir", "videos"),
                "thumbs": c.path("output_dir", "thumbnails")}

    # ------------------------------------------------------------------ run control
    def _params(self, run_id: int) -> dict:
        return (self.repo.get_task(run_id) or {}).get("params_json") or {}

    def _set_params(self, run_id: int, **kw) -> dict:
        p = {**self._params(run_id), **kw}
        self.repo.update_task(run_id, params_json=p)
        return p

    def start(self, topic_id: int | None = None, smoke: bool = False, mode: str | None = None) -> int:
        return self.tm.start_run("smoke_pipeline" if smoke else "daily_pipeline",
                                 {"topic_id": topic_id, "smoke": smoke, "mode": mode or self.cfg.publishing_mode})

    def execute(self, run_id: int) -> dict:
        """Run (or resume) a pipeline run. Returns a summary; never raises for expected stop conditions."""
        params = self._params(run_id)
        smoke = bool(params.get("smoke"))
        mode = (params.get("mode") or self.cfg.publishing_mode).upper()
        self._verify_artifacts(run_id)
        ok, free = self.s.resources.check_disk()
        if not ok:
            self.s.notifier.notify(N.DISK_LOW, "Disk space low", f"{free:.1f} GB free; need {self.cfg.get('pipeline.min_free_disk_gb')} GB", "error")
            self.tm.set_run_status(run_id, TaskStatus.FAILED, error=f"disk space too low ({free:.1f} GB)")
            return self.summary(run_id, "failed", f"disk space too low ({free:.1f} GB)")
        try:
            for stage in STAGES:
                if smoke and stage in ("discover", "analyze", "topic_approval", "script_approval", "upload_approval", "upload"):
                    continue
                self._run_stage(run_id, stage, mode, smoke)
        except WaitingForApproval as w:
            return self.summary(run_id, "waiting_for_approval", w.reason)
        except PipelinePaused:
            return self.summary(run_id, "paused", "paused by user")
        except RepairRequested:
            return self.execute(run_id)          # stages were invalidated; bounded by pipeline.max_qc_repair_attempts
        except StageFailed as e:
            return self.summary(run_id, "failed", str(e))
        self.tm.set_run_status(run_id, TaskStatus.COMPLETED)
        vid = self._params(run_id).get("video_id")
        if vid and not smoke:
            self.s.notifier.notify(N.VIDEO_COMPLETED, "Pipeline finished", f"video #{vid} processed", "success", vid)
        return self.summary(run_id, "completed")

    def summary(self, run_id: int, outcome: str, detail: str = "") -> dict:
        p = self._params(run_id)
        vid = p.get("video_id")
        v = self.repo.get_video(vid) if vid else None
        return {"run_id": run_id, "outcome": outcome, "detail": detail, "video_id": vid,
                "video_status": v["status"] if v else None, "final_path": v.get("final_path") if v else None,
                "stages": [{"stage": t["stage"], "status": t["status"], "retries": t["retry_count"], "error": t["error"]}
                           for t in self.repo.list_stages(run_id)]}

    def _verify_artifacts(self, run_id: int) -> None:
        """If files of a 'completed' stage vanished, re-run that stage and everything after it."""
        for stage in [s for s in STAGES if s in FILE_STAGES]:
            res = self.tm.stage_result(run_id, stage)
            if res is None:
                continue
            bad = False
            for key in FILE_STAGES[stage]:
                val = res.get(key)
                vals = val if isinstance(val, list) else [val]
                if not val or any(not Path(x).exists() for x in vals):
                    bad = True
            if bad:
                log.warning("run %s: outputs of stage '%s' are missing — re-running from there", run_id, stage)
                self.tm.invalidate(run_id, STAGES[STAGES.index(stage):])
                return

    def _run_stage(self, run_id: int, stage: str, mode: str, smoke: bool) -> dict:
        fn = getattr(self, f"stage_{stage}")
        vid = self._params(run_id).get("video_id")
        return self.tm.run_stage(run_id, stage, lambda: fn(run_id, mode, smoke), video_id=vid)

    # ------------------------------------------------------------------ stages
    def stage_discover(self, run_id, mode, smoke):
        if self._params(run_id).get("topic_id"):
            return {"skipped": "topic chosen manually"}
        res = self.s.discovery.discover()
        res.pop("sources", None)
        if res["items_seen"] == 0 and res["trends_measured"] == 0:
            log.warning("no sources returned anything — check `python main.py check-sources`")
        return res

    def stage_analyze(self, run_id, mode, smoke):
        try:
            return self.s.optimizer.refresh()
        except Exception as e:      # analytics are optional input; never block production
            log.warning("performance analysis skipped: %s", e)
            return {"skipped": str(e)}

    def stage_select_topic(self, run_id, mode, smoke):
        if smoke:
            tid = self.repo.add_topic(title=f"Smoke test fixture {run_id}", normalized_title=f"smoke {run_id}",
                                      fingerprint=f"smoke-{run_id}", summary="fixture", status="skipped",
                                      category="black_holes", source_name="smoke-test")
            topic = self.repo.get("topics", tid)
            reason = "smoke-test fixture topic"
        else:
            if self._params(run_id).get("topic_id"):
                sel = self.s.selector.select_specific(int(self._params(run_id)["topic_id"]))
            else:
                sel = self.s.selector.select()
            topic = sel["selected"]
            if not topic:
                raise NonRetryableError("No topic could be selected: " + sel["reason"])
            reason = sel["reason"]
        vid = self.repo.create_video(topic["id"], topic["title"], mode, run_id, is_smoke=smoke)
        self._set_params(run_id, video_id=vid, topic_id=topic["id"])
        return {"topic_id": topic["id"], "video_id": vid, "title": topic["title"], "reason": reason,
                "score": topic.get("score_total")}

    def stage_topic_approval(self, run_id, mode, smoke):
        if mode != "MANUAL":
            return {"skipped": f"mode {mode}"}
        t = self.repo.get("topics", self._params(run_id)["topic_id"])
        self.tm.require_approval(run_id, "topic_approval", f"Approve topic: \"{t['title']}\"", self._params(run_id)["video_id"])
        return {"approved": True}

    def _ctx(self, run_id):
        p = self._params(run_id)
        return p["video_id"], self.repo.get("topics", p["topic_id"])

    def stage_research(self, run_id, mode, smoke):
        vid, topic = self._ctx(run_id)
        d = self.dirs(vid)
        if smoke:
            self.repo.save_research(vid, topic["id"], SMOKE_RESEARCH["summary"], SMOKE_RESEARCH, SMOKE_RESEARCH["sources"], "smoke_fixture")
            rec = SMOKE_RESEARCH
        else:
            rec = self.s.research.research(topic, vid)
        (d["scripts"] / f"{d['stem']}.research.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
        self.repo.update_video(vid, status="researched")
        return {"facts": len(rec["facts"]), "sources": len(rec["sources"]), "method": rec.get("method"), "warnings": rec.get("warnings", [])}

    def stage_script(self, run_id, mode, smoke):
        vid, topic = self._ctx(run_id)
        research = self.repo.get_research(vid)["notes_json"]
        if smoke:
            sections = [{"section": s, "text": t, "visual": "black hole", "sfx": "none"} for s, t in SMOKE_SECTIONS]
            val = self.s.validator.validate(sections)
            narration = " ".join(s["text"] for s in sections)
            fc = self.s.fact_checker.check(narration, research, use_llm=False)
            pkg = self.s.writer._package(sections, "smoke_fixture", None, val, fc, "fixture")
        else:
            pkg = self.s.writer.write(topic, research, vid)
        sid = self.repo.save_script(vid, narration=pkg["narration"], sections_json=pkg["sections"],
                                    estimated_duration=pkg["estimated_duration"], pronunciation_json=pkg["pronunciation"],
                                    captions_json=pkg["captions"], generated_by=pkg["generated_by"], provider=pkg["provider"],
                                    validation_json=pkg["validation"], factcheck_json=pkg["factcheck"])
        d = self.dirs(vid)
        (d["scripts"] / f"{d['stem']}.script.json").write_text(json.dumps(pkg, indent=2, ensure_ascii=False), encoding="utf-8")
        render = {"fallback_content": pkg["generated_by"] == "extractive"}
        self.repo.update_video(vid, status="scripted", render_info_json=render)
        return {"script_id": sid, "words": pkg["validation"]["words"], "duration_est": pkg["estimated_duration"],
                "generated_by": pkg["generated_by"], "provider": pkg["provider"]}

    def stage_script_approval(self, run_id, mode, smoke):
        if mode != "MANUAL":
            return {"skipped": f"mode {mode}"}
        vid, _ = self._ctx(run_id)
        self.tm.require_approval(run_id, "script_approval", f"Approve script of video #{vid}", vid)
        return {"approved": True}

    def stage_scenes(self, run_id, mode, smoke):
        vid, topic = self._ctx(run_id)
        script = self.repo.get_script(vid)
        pkg = {"sections": script["sections_json"], "narration": script["narration"]}
        scenes = self.s.planner.plan(pkg, topic, vid)
        problems = self.s.planner.validate(scenes, script["narration"])
        if problems:
            raise RuntimeError("scene plan invalid: " + "; ".join(problems))
        self.repo.replace_scenes(vid, script["id"], [{k: v for k, v in s.items()} for s in scenes])
        self.repo.update_video(vid, status="scenes_planned")
        return {"scenes": len(scenes), "kinds": [s["visual_kind"] for s in scenes]}

    def stage_voice(self, run_id, mode, smoke):
        vid, _ = self._ctx(run_id)
        d = self.dirs(vid)
        script = self.repo.get_script(vid)
        scenes = self.repo.list_scenes(vid)
        res = self.s.voice.synthesize([{"idx": s["idx"], "narration": s["narration"]} for s in scenes], d["audio"],
                                      script.get("pronunciation_json") or [])
        for sc, tm_ in zip(scenes, res["scenes"]):
            self.repo.update_scene(sc["id"], audio_duration=tm_["audio_duration"], start_time=tm_["start"], duration=tm_["audio_duration"])
        lic = self.repo.add_license(provider="tts", license_name=f"Generated narration ({res['provider']})", author="AutoTube AI",
                                    notes="synthetic voice; disclosed in description")
        self.repo.delete_assets(vid, ("tts",))
        self.repo.add_asset(video_id=vid, kind="audio", method="tts", provider=res["provider"], path=res["path"], license_id=lic,
                            meta_json={"duration": res["duration"], "placeholder": res["placeholder"]})
        (d["audio"] / "voice.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
        v = self.repo.get_video(vid)
        ri = {**(v.get("render_info_json") or {}), "narration_placeholder": res["placeholder"], "voice_provider": res["provider"]}
        self.repo.update_video(vid, status="voiced", render_info_json=ri)
        return {"audio_path": res["path"], "duration": res["duration"], "provider": res["provider"], "timings": res["scenes"]}

    def _timings(self, run_id) -> tuple[list[dict], float]:
        r = self.tm.stage_result(run_id, "voice")
        return r["timings"], r["duration"]

    def stage_visuals(self, run_id, mode, smoke):
        vid, topic = self._ctx(run_id)
        d = self.dirs(vid)
        timings, total = self._timings(run_id)
        settings = self.s.editor.render_settings()
        T = float(self.cfg.get("video.transition_seconds", 0.35))
        lens = clip_lengths([t["end"] for t in timings], total, T)
        scenes = self.repo.list_scenes(vid)
        for old in d["visuals"].glob("scene*.mp4"):
            old.unlink(missing_ok=True)
        self.repo.delete_assets(vid, ("stock_video", "stock_image", "ai_image", "ai_video", "procedural"))
        res = self.s.visuals.produce_all(vid, scenes, d["visuals"], lens, settings["width"], settings["height"], settings["fps"],
                                         settings["crf"], settings["x264_preset"], topic["title"])
        self.repo.update_video(vid, status="visuals_ready")
        return {"clips": [r["path"] for r in res], "methods": [r["method"] for r in res], "providers": [r["provider"] for r in res],
                "synthetic": any(r["synthetic"] for r in res)}

    def stage_captions(self, run_id, mode, smoke):
        vid, _ = self._ctx(run_id)
        d = self.dirs(vid)
        timings, total = self._timings(run_id)
        settings = self.s.editor.render_settings()
        caps = build_captions(timings, self.cfg)
        narration = self.repo.get_script(vid)["narration"]
        val = validate_captions(caps, total, self.cfg, narration)
        srt = write_srt(caps, d["videos"] / f"{d['stem']}.srt", self.cfg)
        ass = write_ass(caps, d["videos"] / f"{d['stem']}.ass", settings["width"], settings["height"], self.cfg)
        self.repo.update_video(vid, srt_path=str(srt))
        return {"srt": str(srt), "ass": str(ass), "count": len(caps), "validation": val}

    def stage_audio(self, run_id, mode, smoke):
        vid, _ = self._ctx(run_id)
        d = self.dirs(vid)
        timings, total = self._timings(run_id)
        scenes = self.repo.list_scenes(vid)
        res = self.s.mixer.mix(self.tm.stage_result(run_id, "voice")["audio_path"], total, timings, scenes, d["audio"], seed=vid)
        self.repo.delete_assets(vid, ("ambient", "music"))
        self.s.mixer.record_assets(vid, res)
        return {"mix_path": res["path"], "music": (res["music"] or {}).get("title"), "credits": res["credits"],
                "mean_db": res["mean_db"], "max_db": res["max_db"]}

    def stage_render(self, run_id, mode, smoke):
        vid, _ = self._ctx(run_id)
        d = self.dirs(vid)
        timings, total = self._timings(run_id)
        vis = self.tm.stage_result(run_id, "visuals")
        cap = self.tm.stage_result(run_id, "captions")
        aud = self.tm.stage_result(run_id, "audio")
        scenes = self.repo.list_scenes(vid)
        out = d["videos"] / f"{d['stem']}.mp4"
        info = self.s.editor.assemble(vis["clips"], [t["end"] for t in timings], [s.get("transition") or "fade" for s in scenes][1:],
                                      aud["mix_path"], cap["ass"], out, total)
        v = self.repo.get_video(vid)
        ri = {**(v.get("render_info_json") or {}), **info, "subtitles_burned": bool(self.cfg.get("subtitles.burn_in", True)),
              "caption_validation": cap["validation"], "visual_methods": vis["methods"]}
        self.repo.update_video(vid, final_path=str(out), duration_seconds=info["duration"], width=info["width"],
                               height=info["height"], render_info_json=ri, status="rendered")
        return {"final": str(out), **{k: info[k] for k in ("duration", "width", "height", "size_mb", "encoder")}}

    def stage_metadata(self, run_id, mode, smoke):
        vid, topic = self._ctx(run_id)
        d = self.dirs(vid)
        script = self.repo.get_script(vid)
        research = self.repo.get_research(vid)["notes_json"]
        vis = self.tm.stage_result(run_id, "visuals")
        meta = self.s.seo.generate(topic, {"narration": script["narration"]}, research, vid, synthetic_media=bool(vis.get("synthetic")))
        credits = self._credits(vid)
        if credits:
            meta["description"] = meta["description"].replace("\n\n" + " ".join(meta["hashtags"]),
                                                               "\n\nCredits:\n" + "\n".join(f"- {c}" for c in credits) + "\n\n" + " ".join(meta["hashtags"]))
        probs = self.s.seo.validate(meta)
        if probs:
            raise RuntimeError("metadata invalid: " + "; ".join(probs))
        self.repo.update_video(vid, title=meta["title"], metadata_json=meta, status="metadata_ready")
        (d["scripts"] / f"{d['stem']}.metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        lic = [{"asset": a["method"], "provider": a["provider"], "source_url": a.get("source_url"),
                "license": (self.repo.get_license(a["license_id"]) if a.get("license_id") else None)} for a in self.repo.list_assets(vid)]
        (d["scripts"] / f"{d['stem']}.sources.json").write_text(json.dumps(lic, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return {"title": meta["title"], "tags": len(meta["tags"]), "used_llm": meta["used_llm"]}

    def _credits(self, vid: int) -> list[str]:
        """Attribution lines for the description (stock credits + any license that requires attribution)."""
        out: list[str] = []
        for a in self.repo.list_assets(vid):
            lic = self.repo.get_license(a["license_id"]) if a.get("license_id") else None
            if not lic or not lic.get("attribution_text"):
                continue
            if (lic.get("attribution_required") or lic.get("provider") in ("pexels", "pixabay", "nasa")) \
                    and lic["attribution_text"] not in out:
                out.append(lic["attribution_text"])
        return out[:8]

    def stage_thumbnail(self, run_id, mode, smoke):
        vid, _ = self._ctx(run_id)
        d = self.dirs(vid)
        meta = self.repo.get_video(vid)["metadata_json"]
        clips = self.tm.stage_result(run_id, "visuals")["clips"]
        out = d["thumbs"] / f"{d['stem']}.jpg"
        res = self.s.thumbs.create(clips, meta["thumbnail_text"], out) if self.cfg.get("thumbnail.enabled", True) else None
        if not res:
            raise ConfigurationError("thumbnail generation is disabled but a thumbnail is required")
        self.repo.update_video(vid, thumbnail_path=str(out))
        return {"thumbnail": str(out), "phrase": res["phrase"], "frame": res["frame"]}

    def stage_qc(self, run_id, mode, smoke):
        vid, _ = self._ctx(run_id)
        report = self.s.qc.inspect(vid)
        attempts = int(self._params(run_id).get("qc_attempts", 0))
        if not report["passed"]:
            only_placeholder = smoke and all("narration_is_real_speech" in r or "quality score" in r for r in report["critical_failures"])
            if only_placeholder:
                return {"passed": False, "smoke_expected_failure": True, "total": report["total"], "scores": report["scores"],
                        "failures": report["critical_failures"]}
            repairable = any(k in " ".join(report["critical_failures"]) for k in
                             ("video_not_corrupted", "no_blank_frames", "frames_have_detail", "duration_in_range", "audio_audible",
                              "format_supported", "aspect_ratio", "video_file_exists", "visual_scenes_exist"))
            if repairable and attempts < int(self.cfg.get("pipeline.max_qc_repair_attempts", 1)):
                self._set_params(run_id, qc_attempts=attempts + 1)
                log.warning("QC failed (%s) — one repair attempt: re-rendering visuals/audio/video", report["critical_failures"])
                self.tm.invalidate(run_id, ["visuals", "audio", "render", "thumbnail", "qc"])
                raise RepairRequested()
            self.repo.update_video(vid, status="qc_failed")
            raise QualityCheckFailed("; ".join(report["critical_failures"]), report)
        return {"passed": True, "total": report["total"], "scores": report["scores"], "warnings": report["warnings"]}

    def _block_flags(self, vid: int) -> list[str]:
        v = self.repo.get_video(vid)
        ri = v.get("render_info_json") or {}
        flags = []
        if ri.get("fallback_content") and self.cfg.get("pipeline.block_auto_publish_for_fallback_content", True):
            flags.append("script was assembled without an AI writer")
        qc = v.get("qc_json") or {}
        if qc.get("warnings"):
            flags.append("QC warnings: " + "; ".join(qc["warnings"])[:160])
        r = self.repo.get_research(vid)
        if r and (r.get("notes_json") or {}).get("warnings"):
            flags.append("research warnings: " + "; ".join(r["notes_json"]["warnings"])[:160])
        return flags

    def stage_upload_approval(self, run_id, mode, smoke):
        vid, _ = self._ctx(run_id)
        flags = self._block_flags(vid)
        needs = mode in ("MANUAL", "REVIEW") or (mode == "AUTO" and flags)
        if not needs:
            return {"skipped": f"mode {mode}, no flags"}
        reason = f"Review and approve upload of video #{vid}" + (f" — flagged: {'; '.join(flags)}" if flags else "")
        self.repo.update_video(vid, status="awaiting_approval")
        self.tm.require_approval(run_id, "upload_approval", reason, vid)
        return {"approved": True, "flags": flags}

    def stage_upload(self, run_id, mode, smoke):
        vid, _ = self._ctx(run_id)
        if self.s.publisher is None:
            raise ConfigurationError("publishing module unavailable (install google-api-python-client, google-auth-oauthlib)")
        approval = self.tm.stage_result(run_id, "upload_approval") or {}
        if mode == "PRIVATE":
            privacy, schedule = "private", False
        else:       # MANUAL/REVIEW (after approval) and AUTO → scheduled public release
            privacy, schedule = "public", True
        res = self.s.publisher.publish(vid, privacy=privacy, schedule=schedule)
        return res
