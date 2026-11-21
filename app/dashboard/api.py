"""FastAPI dashboard backend. Every endpoint calls the real service; nothing here is a placeholder.

Security: loopback bind by default; Host allow-list (DNS-rebinding defence); same-origin check on writes;
optional DASHBOARD_TOKEN (required if bound to a non-loopback address); media served only from output/.
"""
from __future__ import annotations

import copy
import hmac
import shutil
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.errors import AutoTubeError, ConfigurationError
from app.core.logger import get_logger
from app.core.logger import redact

log = get_logger("dashboard")
FRONTEND = Path(__file__).parent / "frontend"

EDITABLE_ROOTS = {"channel", "schedule", "pipeline", "ai", "research", "scoring", "content", "video", "voice", "visuals",
                  "audio", "subtitles", "thumbnail", "seo", "qc", "budget", "notifications", "youtube", "analytics",
                  "optimizer", "monetization", "automation"}
READONLY_PREFIXES = ("paths", "database", "dashboard")


def create_app(svc, scheduler, pipeline_factory) -> FastAPI:
    cfg, repo = svc.cfg, svc.repo
    app = FastAPI(title="AutoTube AI", docs_url=None, redoc_url=None, openapi_url=None)
    token = cfg.env(cfg.get("dashboard.token_env", "DASHBOARD_TOKEN"))
    allowed_hosts = {h.lower() for h in cfg.get("dashboard.allowed_hosts", ["localhost", "127.0.0.1", "[::1]"])}

    # ---------------------------------------------------------------- security middleware
    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower() if not (request.headers.get("host") or "").startswith("[") \
            else (request.headers.get("host") or "").split("]")[0] + "]"
        if allowed_hosts and host not in allowed_hosts and "*" not in allowed_hosts:
            return JSONResponse({"error": "host not allowed"}, status_code=403)
        path = request.url.path
        if path.startswith("/api/") and path != "/api/login":
            if token:
                given = request.headers.get("x-dashboard-token") or request.cookies.get("autotube_token") or ""
                if not hmac.compare_digest(given, token):
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc.lower() != (request.headers.get("host") or "").lower():
                return JSONResponse({"error": "cross-origin request blocked"}, status_code=403)
            if request.headers.get("x-requested-with") != "autotube":
                return JSONResponse({"error": "missing X-Requested-With header"}, status_code=403)
        try:
            resp = await call_next(request)
        except HTTPException:
            raise
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; media-src 'self'; "
                                                   "style-src 'self' 'unsafe-inline'; script-src 'self'")
        return resp

    @app.exception_handler(AutoTubeError)
    async def _autotube_err(request, exc):
        return JSONResponse({"error": redact(str(exc))}, status_code=400)

    @app.exception_handler(Exception)
    async def _any_err(request, exc):
        log.exception("dashboard error on %s", request.url.path)
        return JSONResponse({"error": f"{type(exc).__name__}: {redact(str(exc))[:300]}"}, status_code=500)

    def body(b):
        return b if isinstance(b, dict) else {}

    # ---------------------------------------------------------------- auth
    @app.post("/api/login")
    async def login(payload: dict = Body(...)):
        if not token:
            return {"ok": True, "auth": "disabled"}
        if not hmac.compare_digest(str(payload.get("token", "")), token):
            raise HTTPException(401, "wrong token")
        r = JSONResponse({"ok": True})
        r.set_cookie("autotube_token", token, httponly=True, samesite="strict")
        return r

    # ---------------------------------------------------------------- status
    @app.get("/api/status")
    def status():
        ai_health = svc.ai.health()
        ffm = {"ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe"))}
        yt = svc.auth.status() if svc.auth else {"authorized": False, "note": "google libraries not installed"}
        problems = []
        if not any(h.get("ok") for h in ai_health.values()):
            problems.append("No AI provider is reachable. Start Ollama (ollama serve) or llama.cpp server - "
                            "without it the system uses a limited no-AI fallback and will never auto-publish.")
        if not all(ffm.values()):
            problems.append("FFmpeg/ffprobe not found on PATH - videos cannot be rendered.")
        voices = svc.voice.status()
        if not any(v["available"] and not v["placeholder"] for v in voices):
            problems.append("No real TTS voice available. Run: python main.py setup-voice")
        if not yt.get("authorized"):
            problems.append("YouTube is not connected (Settings -> Connect YouTube). Videos can still be produced locally.")
        for e in cfg.validate():
            problems.append("Config: " + e)
        return {"channel": cfg.get("channel.name"), "publishing_mode": cfg.publishing_mode, "timezone": cfg.get("channel.timezone"),
                "ai": ai_health, "tools": ffm, "voices": voices, "youtube": yt, "resources": svc.resources.snapshot(),
                "budget": svc.budget.summary(), "scheduler": scheduler.status(), "problems": problems,
                "counts": {"topics": len(repo.list_topics(limit=1000)), "videos": len(repo.list_videos(limit=1000)),
                           "waiting_approvals": len(svc.tm.waiting_approvals())}}

    # ---------------------------------------------------------------- config
    @app.get("/api/config")
    def get_config():
        return cfg.public_dict()

    @app.post("/api/config")
    def set_config(updates: dict = Body(...)):
        if not isinstance(updates, dict) or not updates:
            raise HTTPException(400, "send a JSON object of settings")
        for k in updates:
            if k in READONLY_PREFIXES or k not in EDITABLE_ROOTS:
                raise HTTPException(400, f"setting group '{k}' cannot be changed from the dashboard")
        before = copy.deepcopy(cfg.data)
        from app.core.config import deep_merge
        cfg.data = deep_merge(cfg.data, updates)
        errs = cfg.validate()
        cfg.data = before
        if errs:
            raise HTTPException(400, "; ".join(errs))
        cfg.save_local_override(updates)
        return {"saved": True}

    @app.get("/api/keys")
    def keys():
        from app.core.secrets import key_status
        return {"keys": key_status(cfg), "how_to": "Keys are never entered in the browser. Run: python main.py set-key NAME"}

    @app.get("/api/config/channel")
    def channel_cfg():
        return cfg.get("channel")

    # ---------------------------------------------------------------- topics
    @app.get("/api/topics")
    def topics(status: str | None = None, limit: int = 100):
        return repo.list_topics(status, min(limit, 500))

    @app.post("/api/topics/discover")
    def discover():
        ok = scheduler.submit("topic discovery", lambda: svc.discovery.discover(), key="discover")
        return {"queued": ok}

    @app.post("/api/topics/score")
    def score():
        ok = scheduler.submit("topic scoring", lambda: svc.selector.select(), key="score")
        return {"queued": ok}

    @app.post("/api/topics/{tid}/reject")
    def reject_topic(tid: int, payload: dict = Body(default={})):
        if not repo.get("topics", tid):
            raise HTTPException(404)
        repo.set_topic_status(tid, "rejected", rejection_reason=str(body(payload).get("reason", "rejected by user"))[:200])
        return {"ok": True}

    @app.post("/api/topics/{tid}/produce")
    def produce_topic(tid: int, payload: dict = Body(default={})):
        if not repo.get("topics", tid):
            raise HTTPException(404)
        rid = scheduler.run_pipeline_now(topic_id=tid, mode=body(payload).get("mode"))
        return {"run_id": rid}

    @app.post("/api/topics/add")
    def add_topic(payload: dict = Body(...)):
        title = str(payload.get("title", "")).strip()
        if len(title) < 8:
            raise HTTPException(400, "title is too short")
        from app.utils.text import fingerprint, keywords, normalize
        tid = repo.add_topic(title=title, normalized_title=normalize(title), fingerprint=fingerprint(title),
                             summary=str(payload.get("summary", ""))[:1000], source_name="manual",
                             category=payload.get("category"), keywords=keywords(title, 6))
        if not tid:
            raise HTTPException(409, "a similar topic already exists")
        return {"id": tid}

    # ---------------------------------------------------------------- pipeline / runs
    @app.post("/api/pipeline/run")
    def run_pipeline(payload: dict = Body(default={})):
        p = body(payload)
        mode = p.get("mode")
        if mode and str(mode).upper() not in ("MANUAL", "REVIEW", "PRIVATE", "AUTO"):
            raise HTTPException(400, "invalid mode")
        rid = scheduler.run_pipeline_now(topic_id=p.get("topic_id"), smoke=bool(p.get("smoke")), mode=str(mode).upper() if mode else None)
        return {"run_id": rid}

    @app.get("/api/runs")
    def runs(limit: int = 30):
        out = []
        for r in repo.list_tasks(kind="pipeline", limit=min(limit, 100)):
            r["stages"] = [{"stage": s["stage"], "status": s["status"], "error": s["error"], "retries": s["retry_count"]}
                           for s in repo.list_stages(r["id"])]
            out.append(r)
        return out

    @app.get("/api/runs/{rid}")
    def run_detail(rid: int):
        t = repo.get_task(rid)
        if not t:
            raise HTTPException(404)
        t["stages"] = repo.list_stages(rid)
        return t

    @app.post("/api/runs/{rid}/pause")
    def pause(rid: int):
        svc.tm.pause_run(rid)
        return {"ok": True}

    @app.post("/api/runs/{rid}/resume")
    def resume(rid: int):
        svc.tm.resume_run(rid)
        return {"queued": scheduler.resume_run(rid)}

    @app.get("/api/approvals")
    def approvals():
        out = []
        for t in svc.tm.waiting_approvals():
            v = repo.get_video(t["video_id"]) if t.get("video_id") else None
            out.append({"run_id": t["parent_id"], "stage": t["stage"], "reason": (t.get("params_json") or {}).get("reason"),
                        "video": ({"id": v["id"], "title": v["title"], "status": v["status"]} if v else None)})
        return out

    @app.post("/api/runs/{rid}/approve")
    def approve(rid: int, payload: dict = Body(...)):
        stage, ok = str(payload.get("stage", "")), bool(payload.get("approved", True))
        if not svc.tm.approve(rid, stage, ok):
            raise HTTPException(409, "that stage is not waiting for approval")
        if ok:
            scheduler.resume_run(rid)
        return {"ok": True, "resumed": ok}

    # ---------------------------------------------------------------- videos
    @app.get("/api/videos")
    def videos(status: str | None = None, limit: int = 100):
        out = []
        for v in repo.list_videos(status, min(limit, 300)):
            up = repo.uploads_for_video(v["id"])
            out.append({k: v.get(k) for k in ("id", "title", "status", "publish_mode", "duration_seconds", "quality_score",
                                               "is_smoke", "created_at", "run_id")} | {"upload": up[0] if up else None,
                                                                                       "has_file": bool(v.get("final_path") and Path(v["final_path"]).is_file())})
        return out

    @app.get("/api/videos/{vid}")
    def video(vid: int):
        v = repo.get_video(vid)
        if not v:
            raise HTTPException(404)
        v["script"] = repo.get_script(vid)
        v["research"] = repo.get_research(vid)
        v["scenes"] = repo.list_scenes(vid)
        v["assets"] = repo.list_assets(vid)
        v["uploads"] = repo.uploads_for_video(vid)
        v["licenses"] = [repo.get_license(a["license_id"]) for a in v["assets"] if a.get("license_id")]
        return v

    def _media(path_str: str | None, kind: str):
        if not path_str:
            raise HTTPException(404)
        p = Path(path_str).resolve()
        root = cfg.path("output_dir").resolve()
        if root not in p.parents or not p.is_file():
            raise HTTPException(404)
        return FileResponse(p, media_type={"video": "video/mp4", "image": "image/jpeg"}[kind])

    @app.get("/media/video/{vid}")
    def media_video(vid: int):
        v = repo.get_video(vid)
        return _media(v and v.get("final_path"), "video")

    @app.get("/media/thumb/{vid}")
    def media_thumb(vid: int):
        v = repo.get_video(vid)
        return _media(v and v.get("thumbnail_path"), "image")

    @app.post("/api/videos/{vid}/qc")
    def rerun_qc(vid: int):
        if not repo.get_video(vid):
            raise HTTPException(404)
        return svc.qc.inspect(vid)

    @app.post("/api/videos/{vid}/upload")
    def upload_now(vid: int, payload: dict = Body(default={})):
        """Manual upload of an already QC-passed video (MANUAL mode workflow)."""
        if svc.publisher is None:
            raise HTTPException(400, "publishing libraries not installed")
        p = body(payload)
        privacy = str(p.get("privacy", "private"))
        if privacy not in ("private", "unlisted", "public"):
            raise HTTPException(400, "invalid privacy")
        schedule = bool(p.get("schedule", False)) and privacy == "public"
        ok = scheduler.submit(f"upload video {vid}", lambda: svc.publisher.publish(vid, privacy=privacy, schedule=schedule), key=f"upload:{vid}")
        return {"queued": ok}

    # ---------------------------------------------------------------- YouTube
    @app.get("/api/youtube/status")
    def yt_status():
        return svc.auth.status() if svc.auth else {"authorized": False}

    @app.post("/api/youtube/connect")
    def yt_connect():
        if not svc.auth:
            raise HTTPException(400, "google libraries not installed")
        if not svc.auth.client_secrets_path() or not svc.auth.client_secrets_path().is_file():
            raise HTTPException(400, "YOUTUBE_CLIENT_SECRETS is not set or the file does not exist (see README -> YouTube setup)")
        started = svc.auth.authorize_in_background()
        return {"started": started, "note": "A browser window opens on the computer running AutoTube AI. Finish consent there."}

    @app.post("/api/youtube/disconnect")
    def yt_disconnect():
        return {"disconnected": bool(svc.auth and svc.auth.disconnect())}

    @app.get("/api/youtube/uploads")
    def uploads():
        return repo.list_uploads(100)

    @app.post("/api/youtube/refresh")
    def yt_refresh():
        if not svc.publisher:
            raise HTTPException(400, "publishing unavailable")
        return {"changed": svc.publisher.refresh_statuses()}

    # ---------------------------------------------------------------- analytics
    @app.get("/api/analytics")
    def analytics():
        vids = {v["id"]: v for v in repo.list_videos(limit=500)}
        rows = [a | {"title": (vids.get(a.get("video_id")) or {}).get("title")} for a in repo.latest_analytics()]
        return {"videos": rows, "channel": repo.channel_stats_history(60),
                "notes": ["Impressions/CTR are not exposed by the YouTube Analytics API (shown as n/a).",
                          "Revenue appears only if YouTube reports it for a monetized channel."]}

    @app.post("/api/analytics/collect")
    def collect():
        if not svc.collector_analytics:
            raise HTTPException(400, "analytics libraries not installed")
        return {"queued": scheduler.submit("collect analytics", scheduler.job_analytics, key="analytics")}

    @app.get("/api/reports/{kind}")
    def report(kind: str, save: bool = False):
        if kind not in ("daily", "weekly"):
            raise HTTPException(404)
        return svc.reports.generate(kind, save=save)

    @app.post("/api/optimizer/run")
    def optimizer(payload: dict = Body(default={})):
        apply = body(payload).get("apply")
        return svc.optimizer.run(apply=apply if isinstance(apply, bool) else None)

    @app.get("/api/strategy")
    def strategy():
        return {"weights": repo.get_setting("topic_weights", {}) or {}, "changes": repo.list_strategy_changes(50)}

    @app.post("/api/strategy/reset")
    def strategy_reset():
        repo.set_setting("topic_weights", {})
        repo.add_strategy_change("topic_weights", {"reset": True}, "reset by user", auto_applied=False, status="applied")
        return {"ok": True}

    @app.get("/api/monetization")
    def monetization():
        if not svc.collector_analytics:
            raise HTTPException(400, "analytics libraries not installed")
        return svc.collector_analytics.monetization_progress()

    # ---------------------------------------------------------------- ops
    @app.post("/api/automation/{action}")
    def automation(action: str):
        if action not in ("start", "stop"):
            raise HTTPException(404)
        scheduler.set_automation(action == "start")
        return {"automation_enabled": scheduler.automation_enabled}

    @app.get("/api/logs")
    def logs(level: str | None = None, category: str | None = None, limit: int = 200):
        return repo.list_logs(level, category, min(limit, 1000))

    @app.get("/api/notifications")
    def notifications(unread: bool = False):
        return repo.list_notifications(100, unread)

    @app.post("/api/notifications/read")
    def notifications_read():
        repo.mark_notifications_read()
        return {"ok": True}

    @app.get("/api/providers/check")
    def providers_check():
        return {"ai": svc.ai.health(force=True), "voices": svc.voice.status()}

    @app.get("/api/sources")
    def sources():
        return repo.list_sources()

    @app.get("/api/budget")
    def budget():
        return svc.budget.summary()

    @app.get("/api/queue")
    def queue_status():
        return scheduler.status()

    # ---------------------------------------------------------------- frontend
    @app.get("/")
    def index():
        return FileResponse(FRONTEND / "index.html", headers={"Cache-Control": "no-store"})

    if FRONTEND.is_dir():
        app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
    return app
