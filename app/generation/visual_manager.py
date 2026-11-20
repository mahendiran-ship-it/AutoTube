"""Visual manager: per-scene provider chain with fallbacks. The chain NEVER ends in failure:
`procedural` is the always-available last resort. Every scene records which method produced it."""
from __future__ import annotations

import shutil
from pathlib import Path

from app.core.errors import BudgetExceeded
from app.core.logger import get_logger
from app.generation.image_motion import image_to_clip
from app.generation.image_provider import AutomaticImageProvider
from app.generation.procedural_visuals import render_procedural, seed_from, TimelineScene
from app.generation.stock_provider import StockManager
from app.generation.video_provider import CommandVideoProvider
from app.generation.visual_kinds import SEARCH_HINTS
from app.utils import ffmpeg as ff
from app.utils.validators import slugify

log = get_logger("visuals")


def normalize_clip(src: Path, dst: Path, duration: float, width: int, height: int, fps: int, crf: int, preset: str) -> Path:
    """Any video → exact-length, exact-size, constant-fps, silent H.264 clip (loops if the source is too short)."""
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,crop={width}:{height},"
          f"fps={fps},setsar=1,format=yuv420p")
    ff.run(["-stream_loop", "-1", "-i", str(src), "-t", f"{duration:.3f}", "-vf", vf, "-an", "-c:v", "libx264",
            "-preset", preset, "-crf", str(crf), "-movflags", "+faststart", str(dst)])
    return dst


class VisualManager:
    def __init__(self, cfg, repo, budget=None, stock: StockManager | None = None, resources=None,
                 image_ai: AutomaticImageProvider | None = None, video_ai: CommandVideoProvider | None = None):
        self.cfg, self.repo, self.budget, self.resources = cfg, repo, budget, resources
        self.stock = stock or StockManager(cfg, repo)
        self.image_ai = image_ai or AutomaticImageProvider(cfg)
        self.video_ai = video_ai or CommandVideoProvider(cfg)

    # ---- helpers -------------------------------------------------------
    def order(self) -> list[str]:
        order = [o for o in (self.cfg.get("visuals.provider_order") or []) if o != "procedural"]
        return order + ["procedural"]            # guaranteed safety net

    def status(self) -> dict:
        return {
            "video_ai": {"enabled": self.video_ai.enabled(), "detail": self.video_ai.health()[1]},
            "stock": {p.name: p.enabled() for p in self.stock.providers},
            "image_ai": {"enabled": self.image_ai.enabled(), "detail": self.image_ai.health()[1] if self.image_ai.enabled() else "disabled"},
            "procedural": {"enabled": True, "detail": "always available"},
            "order": self.order(),
        }

    def _store_license(self, c) -> int:
        return self.repo.add_license(**c.license_record())

    # ---- one scene -----------------------------------------------------
    def produce(self, scene: dict, video_id: int, out_dir: Path, clip_seconds: float, width: int, height: int,
                fps: int, crf: int, preset: str, topic_text: str = "") -> dict:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        base = f"scene{scene['idx']:02d}"
        final = out_dir / f"{base}.mp4"
        attempts: list[dict] = []
        kind = scene.get("visual_kind") or "starfield"
        query = scene.get("search_query") or SEARCH_HINTS.get(kind, "space")
        if "space" not in query.lower() and kind in SEARCH_HINTS:
            query = f"{query} {SEARCH_HINTS[kind].split()[-1]}"
        extra = [kind.replace("_", " ")]
        seed = seed_from(f"{video_id}:{scene['idx']}:{scene['narration']}")

        def done(method, provider, **kw):
            return {"path": str(final), "method": method, "provider": provider, "attempts": attempts,
                    "synthetic": kw.pop("synthetic", False), **kw}

        for method in self.order():
            try:
                if method == "video_ai" and self.video_ai.enabled():
                    if self.budget:
                        self.budget.authorize("video_ai", self.video_ai.is_paid)
                    raw = out_dir / f"{base}_ai_raw.mp4"
                    self.video_ai.generate(scene.get("visual_prompt") or scene["visual_description"], raw, clip_seconds, width, height)
                    normalize_clip(raw, final, clip_seconds, width, height, fps, crf, preset)
                    attempts.append({"method": method, "ok": True})
                    return done("ai_video", "video_ai_command", synthetic=True)

                if method == "stock_video" and self.stock.active():
                    cands = self.stock.find("video", query, extra, video_id, clip_seconds)
                    for c in cands[:3]:
                        try:
                            if self.resources and not self.resources.can_download(150_000_000):
                                raise RuntimeError("not enough free disk for download")
                            raw = self.stock.download(c, out_dir, f"{base}_{c.provider}_raw.mp4")
                            info = ff.probe(raw)
                            if not info["has_video"] or info["duration"] < 2.5:
                                raw.unlink(missing_ok=True)
                                raise RuntimeError("downloaded clip invalid or too short")
                            normalize_clip(raw, final, clip_seconds, width, height, fps, crf, preset)
                            lic = self._store_license(c)
                            attempts.append({"method": method, "ok": True, "provider": c.provider})
                            return done("stock_video", c.provider, source_url=c.page_url or c.url, license_id=lic,
                                        meta={"author": c.author, "source_duration": info["duration"]})
                        except Exception as e:
                            attempts.append({"method": method, "ok": False, "provider": c.provider, "detail": str(e)[:160]})
                    if not cands:
                        attempts.append({"method": method, "ok": False, "detail": "no relevant licensed footage found"})
                    continue

                if method == "stock_image" and self.stock.active():
                    cands = self.stock.find("image", query, extra, video_id)
                    for c in cands[:3]:
                        try:
                            raw = self.stock.download(c, out_dir, f"{base}_{c.provider}_img")
                            image_to_clip(raw, final, clip_seconds, width, height, fps, seed, scene.get("camera", ""), crf, preset)
                            lic = self._store_license(c)
                            attempts.append({"method": method, "ok": True, "provider": c.provider})
                            return done("stock_image", c.provider, source_url=c.page_url or c.url, license_id=lic,
                                        meta={"author": c.author, "motion": "ken_burns+dust"})
                        except Exception as e:
                            attempts.append({"method": method, "ok": False, "provider": c.provider, "detail": str(e)[:160]})
                    if not cands:
                        attempts.append({"method": method, "ok": False, "detail": "no relevant licensed images found"})
                    continue

                if method == "image_ai" and self.image_ai.enabled():
                    ok, why = self.image_ai.health()
                    if not ok:
                        attempts.append({"method": method, "ok": False, "detail": why})
                        continue
                    if self.resources:
                        can, why2 = self.resources.can_load_model(0.5)
                        if not can:
                            attempts.append({"method": method, "ok": False, "detail": why2})
                            continue
                    img = self.image_ai.generate(scene.get("visual_prompt") or scene["visual_description"], out_dir / f"{base}_ai.png")
                    image_to_clip(img, final, clip_seconds, width, height, fps, seed, scene.get("camera", ""), crf, preset)
                    attempts.append({"method": method, "ok": True})
                    return done("ai_image", self.image_ai.name, synthetic=True, meta={"motion": "ken_burns+dust"})

                if method == "procedural":
                    k = kind
                    if k == "timeline" and len(TimelineScene.labels_from(scene["narration"])) < 2:
                        k = "starfield"
                    scale = float(self.cfg.get("visuals.procedural.internal_scale", 0.5))
                    iw, ih = max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)
                    raw = out_dir / f"{base}_proc_raw.mp4"
                    meta = render_procedural(k, raw, clip_seconds, iw, ih, fps, seed, scene.get("camera", ""),
                                             f"{topic_text} {scene['narration']}", crf=16, preset=preset)
                    normalize_clip(raw, final, clip_seconds, width, height, fps, crf, preset)
                    raw.unlink(missing_ok=True)
                    attempts.append({"method": method, "ok": True})
                    return done("procedural", "procedural", synthetic=False, meta={**meta, "note": "artistic illustration"})
            except BudgetExceeded as e:
                attempts.append({"method": method, "ok": False, "detail": f"blocked by budget: {e}"})
            except Exception as e:
                log.warning("scene %s: visual method %s failed: %s", scene["idx"], method, e)
                attempts.append({"method": method, "ok": False, "detail": f"{type(e).__name__}: {str(e)[:160]}"})
        raise RuntimeError("all visual methods failed including procedural: " + str(attempts))   # pragma: no cover

    def produce_all(self, video_id: int, scenes: list[dict], out_dir: Path, clip_lengths: list[float], width: int,
                    height: int, fps: int, crf: int, preset: str, topic_text: str = "", progress=None) -> list[dict]:
        results = []
        for sc, secs in zip(scenes, clip_lengths):
            res = self.produce(sc, video_id, out_dir, secs, width, height, fps, crf, preset, topic_text)
            asset_id = self.repo.add_asset(video_id=video_id, scene_id=sc.get("id"), kind="clip", method=res["method"],
                                           provider=res["provider"], path=res["path"], source_url=res.get("source_url"),
                                           license_id=res.get("license_id"),
                                           meta_json={"attempts": res["attempts"], "synthetic": res["synthetic"],
                                                      **(res.get("meta") or {})})
            if sc.get("id"):
                self.repo.update_scene(sc["id"], asset_id=asset_id, visual_method=res["method"])
            res["asset_id"] = asset_id
            results.append(res)
            if progress:
                progress(sc["idx"], res["method"])
        return results
