#!/usr/bin/env python3
"""AutoTube AI command line.   python main.py --help"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.core.config import Config  # noqa: E402
from app.core.errors import AutoTubeError  # noqa: E402


def load() -> Config:
    return Config.load()


def services(cfg):
    from app.pipeline.services import build_services
    return build_services(cfg)


def pipeline(svc):
    from app.pipeline.orchestrator import Pipeline
    return Pipeline(svc)


def pp(obj) -> None:
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


# ------------------------------------------------------------------ commands
def cmd_init(a):
    cfg = load()
    for k in ("data_dir", "output_dir", "secrets_dir", "logs_dir"):
        cfg.path(k)
    env, ex = ROOT / ".env", ROOT / ".env.example"
    if not env.exists() and ex.exists():
        shutil.copy(ex, env)
        print("created .env from .env.example (edit it to add optional keys)")
    svc = services(cfg)
    print(f"database ready: {cfg.db_path}")
    errs = cfg.validate()
    print("config OK" if not errs else "config problems:\n  - " + "\n  - ".join(errs))
    return 1 if errs else 0


def cmd_doctor(a):
    cfg = load()
    svc = services(cfg)
    ok = True
    print(f"Python {sys.version.split()[0]}")
    for t in ("ffmpeg", "ffprobe"):
        p = shutil.which(t)
        print(f"{'OK ' if p else 'MISSING'} {t}: {p or 'install it and add to PATH'}")
        ok &= bool(p)
    for name, h in svc.ai.health(force=True).items():
        print(f"{'OK ' if h.get('ok') else 'DOWN'} AI provider {name}: {h.get('detail', '')}")
    for v in svc.voice.status():
        print(f"{'OK ' if v['available'] else 'DOWN'} voice {v['name']}: {v['detail']}{' (placeholder)' if v['placeholder'] else ''}")
    print("disk free: %.1f GB" % svc.resources.disk_free_gb())
    if svc.auth:
        st = svc.auth.status()
        print(f"YouTube: client file {'found' if st['client_secrets_configured'] else 'missing'}, "
              f"{'connected' if st['authorized'] else 'not connected'}")
    for e in cfg.validate():
        print("CONFIG PROBLEM:", e)
        ok = False
    return 0 if ok else 1


def cmd_setup_voice(a):
    cfg = load()
    model = Path(cfg.get("voice.piper.model", "data/voices/en_GB-alan-medium.onnx"))
    model = model if model.is_absolute() else ROOT / model
    voice = a.voice or model.stem
    model.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "piper.download_voices", voice, "--download-dir", str(model.parent)]
    print("running:", " ".join(cmd))
    try:
        r = subprocess.run(cmd, timeout=900)
    except FileNotFoundError:
        r = None
    if r is None or r.returncode != 0:
        print("\nCould not download automatically. Install Piper first:  pip install piper-tts\n"
              "or download the .onnx and .onnx.json manually from https://huggingface.co/rhasspy/piper-voices\n"
              f"and place both files in {model.parent}")
        return 1
    print("voice ready:", model, "exists" if model.is_file() else "(file not found - check the voice name)")
    return 0 if model.is_file() else 1


def cmd_check_sources(a):
    svc = services(load())
    items, report = svc.collector.collect()
    for r in report:
        print(f"{r['status'].upper():8} {r.get('name')}: {r.get('items', 0)} items {r.get('error') or ''}")
    print(f"{len(items)} items total")
    return 0


def cmd_discover(a):
    svc = services(load())
    pp(svc.discovery.discover())
    if a.select:
        pp({k: v for k, v in svc.selector.select().items() if k != "ranking"})
    return 0


def cmd_smoke(a):
    svc = services(load())
    pl = pipeline(svc)
    res = pl.execute(pl.start(smoke=True))
    pp(res)
    print("\nSmoke videos use fixture text and (without a real voice) a placeholder tone: they are never uploaded.")
    return 0 if res["outcome"] == "completed" else 1


def cmd_run(a):
    svc = services(load())
    pl = pipeline(svc)
    res = pl.execute(pl.start(topic_id=a.topic, mode=a.mode))
    pp(res)
    return 0 if res["outcome"] in ("completed", "waiting_for_approval") else 1


def cmd_resume(a):
    svc = services(load())
    pp(pipeline(svc).execute(a.run_id))
    return 0


def cmd_approve(a):
    svc = services(load())
    if not svc.tm.approve(a.run_id, a.stage, not a.reject):
        print("that stage is not waiting for approval")
        return 1
    if not a.reject:
        pp(pipeline(svc).execute(a.run_id))
    return 0


def cmd_youtube_auth(a):
    svc = services(load())
    if not svc.auth:
        print("Google libraries missing: pip install -r requirements.txt")
        return 1
    svc.auth.authorize(open_browser=not a.no_browser)
    pp(svc.auth.status())
    return 0


def cmd_analytics(a):
    svc = services(load())
    if not svc.collector_analytics:
        print("Google libraries missing")
        return 1
    pp(svc.collector_analytics.collect_all())
    return 0


def cmd_report(a):
    svc = services(load())
    r = svc.reports.generate(a.kind)
    print(r["markdown"])
    print("\nsaved:", r.get("path"))
    return 0


def cmd_optimize(a):
    svc = services(load())
    r = svc.optimizer.run(apply=False if a.dry_run else None)
    pp(r)
    return 0


def cmd_keys(a):
    from app.core.secrets import key_status
    cfg = load()
    for k in key_status(cfg):
        print(f"{'SET    ' if k['set'] else 'not set'}  {k['name']:<20} {k['purpose']}")
    print("\nAdd one with:  python main.py set-key NAME   (input is hidden; saved only to .env)")
    return 0


def cmd_set_key(a):
    import getpass

    from app.core.secrets import set_env_key
    value = a.value or getpass.getpass(f"Paste value for {a.name.upper()} (hidden): ")
    path = set_env_key(ROOT, a.name, value)
    print(f"saved {a.name.upper()} to {path} (never committed; the file is git-ignored)")
    return 0


def cmd_use_provider(a):
    cfg = load()
    prov = (cfg.get("ai.providers") or {})
    if a.name not in prov:
        print(f"unknown provider '{a.name}'. Available: {', '.join(prov)}")
        return 1
    upd = {"ai": {"providers": {a.name: {"enabled": True}}}}
    if a.model:
        upd["ai"]["providers"][a.name]["model"] = a.model
    order = [a.name] + [n for n in (cfg.get("ai.fallback_order") or []) if n != a.name]
    upd["ai"]["fallback_order"] = order
    cfg.save_local_override(upd)
    env = prov[a.name].get("api_key_env")
    print(f"'{a.name}' enabled and set as first choice (order: {', '.join(order)}).")
    if env and not cfg.env(env):
        print(f"!! {env} is not set yet. Run:  python main.py set-key {env}")
    print("Check it with:  python main.py doctor")
    return 0


def cmd_serve(a):
    import uvicorn

    from app.core.scheduler import Scheduler
    from app.dashboard.api import create_app
    cfg = load()
    host, port = a.host or cfg.get("dashboard.host", "127.0.0.1"), a.port or int(cfg.get("dashboard.port", 8787))
    token_env = cfg.get("dashboard.token_env", "DASHBOARD_TOKEN")
    if host not in ("127.0.0.1", "localhost", "::1") and not cfg.env(token_env):
        print(f"Refusing to bind to {host} without {token_env} set in .env (the dashboard controls your channel).")
        return 1
    svc = services(cfg)
    sched = Scheduler(svc, lambda: pipeline(svc))
    sched.start()
    app = create_app(svc, sched, lambda: pipeline(svc))
    print(f"\n  AutoTube AI dashboard: http://{host}:{port}\n  Press Ctrl+C to stop.\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


def cmd_scheduler(a):
    """Headless: run the scheduler without the dashboard."""
    import time

    from app.core.scheduler import Scheduler
    svc = services(load())
    sched = Scheduler(svc, lambda: pipeline(svc))
    sched.set_automation(True)
    sched.start()
    print("scheduler running (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        sched.stop()
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="autotube", description="AutoTube AI - autonomous YouTube Shorts pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, **kw):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp
    add("init", cmd_init, "create folders, .env and the database")
    add("doctor", cmd_doctor, "check FFmpeg, AI, voice, YouTube, config")
    s = add("setup-voice", cmd_setup_voice, "download the Piper voice model")
    s.add_argument("--voice")
    add("keys", cmd_keys, "show which API keys are set (never prints values)")
    s = add("set-key", cmd_set_key, "save an API key to .env (hidden input)")
    s.add_argument("name")
    s.add_argument("--value", help="avoid: visible in shell history; omit to be prompted")
    s = add("use-provider", cmd_use_provider, "enable an AI provider and make it the first choice")
    s.add_argument("name")
    s.add_argument("--model")
    add("check-sources", cmd_check_sources, "fetch all RSS sources and report which work")
    s = add("discover", cmd_discover, "discover topics (and optionally score/select)")
    s.add_argument("--select", action="store_true")
    add("smoke-test", cmd_smoke, "render a fixture video through the real pipeline (never uploads)")
    s = add("run", cmd_run, "run the full pipeline once")
    s.add_argument("--topic", type=int)
    s.add_argument("--mode", choices=["MANUAL", "REVIEW", "PRIVATE", "AUTO"])
    s = add("resume", cmd_resume, "resume a pipeline run")
    s.add_argument("run_id", type=int)
    s = add("approve", cmd_approve, "approve (or --reject) a waiting stage and continue")
    s.add_argument("run_id", type=int)
    s.add_argument("stage")
    s.add_argument("--reject", action="store_true")
    s = add("youtube-auth", cmd_youtube_auth, "connect your YouTube account")
    s.add_argument("--no-browser", action="store_true")
    add("analytics", cmd_analytics, "collect YouTube analytics now")
    s = add("report", cmd_report, "print a daily/weekly report")
    s.add_argument("kind", nargs="?", default="daily", choices=["daily", "weekly"])
    s = add("optimize", cmd_optimize, "run the performance optimizer")
    s.add_argument("--dry-run", action="store_true")
    s = add("serve", cmd_serve, "start the dashboard + scheduler")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    add("scheduler", cmd_scheduler, "run the scheduler headless (no dashboard)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except AutoTubeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
