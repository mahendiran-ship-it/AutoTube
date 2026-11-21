"""In-process scheduler + single-worker job queue.

- One worker thread runs jobs one at a time (a 2-CPU / 8 GB machine must not render two videos at once).
- Daily jobs fire once per local day at their configured time; the "last run" date is stored in the DB,
  so a restart does not repeat them and a missed time (PC was off) is still run later the same day.
- Automation is OFF until the user turns it on (dashboard or `main.py run-scheduler`).
"""
from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timezone

from app.core.logger import get_logger
from app.publishing.scheduler import parse_hhmm

log = get_logger("scheduler")


class Scheduler:
    def __init__(self, svc, pipeline_factory):
        self.s = svc
        self.cfg, self.repo = svc.cfg, svc.repo
        self._pipeline_factory = pipeline_factory
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._ticker: threading.Thread | None = None
        self.current: dict | None = None
        self.history: list[dict] = []
        self._queued: set[str] = set()
        self._lock = threading.Lock()

    # ---- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._work, daemon=True, name="job-worker")
        self._worker.start()
        self._ticker = threading.Thread(target=self._tick_loop, daemon=True, name="scheduler-tick")
        self._ticker.start()
        for rid in self.s.tm.recover_interrupted():          # resume runs interrupted by a crash
            self.submit(f"resume run {rid}", lambda rid=rid: self._pipeline_factory().execute(rid), key=f"run:{rid}")

    def stop(self) -> None:
        self._stop.set()

    @property
    def automation_enabled(self) -> bool:
        return bool(self.repo.get_setting("automation_enabled", self.cfg.get("automation.enabled", False)))

    def set_automation(self, on: bool) -> None:
        self.repo.set_setting("automation_enabled", bool(on))
        log.info("automation %s", "enabled" if on else "disabled")

    # ---- queue -------------------------------------------------------------------
    def submit(self, name: str, fn, key: str | None = None) -> bool:
        """Queue a job. A job with the same key that is queued or running is not queued twice."""
        key = key or name
        with self._lock:
            if key in self._queued:
                return False
            self._queued.add(key)
        self._q.put((name, key, fn))
        return True

    def status(self) -> dict:
        return {"worker_alive": bool(self._worker and self._worker.is_alive()), "current": self.current,
                "queued": self._q.qsize(), "automation_enabled": self.automation_enabled,
                "history": self.history[-10:]}

    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                name, key, fn = self._q.get(timeout=1)
            except queue.Empty:
                continue
            self.current = {"name": name, "started": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            outcome = "ok"
            try:
                res = fn()
                if isinstance(res, dict) and res.get("outcome") in ("failed",):
                    outcome = "failed: " + str(res.get("detail", ""))[:200]
            except Exception as e:
                outcome = f"error: {type(e).__name__}: {str(e)[:200]}"
                log.exception("job '%s' failed", name)
            finally:
                self.history.append({"name": name, "outcome": outcome, "finished": datetime.now(timezone.utc).isoformat(timespec="seconds")})
                self.history = self.history[-50:]
                self.current = None
                with self._lock:
                    self._queued.discard(key)

    # ---- daily jobs -----------------------------------------------------------------
    def _now_local(self) -> datetime:
        return datetime.now(timezone.utc).astimezone(self.cfg.tz)

    def _due(self, job: str, at: str, weekday: int | None = None) -> bool:
        now = self._now_local()
        if weekday is not None and now.weekday() != int(weekday):
            return False
        if now.time() < parse_hhmm(at):
            return False
        return self.repo.get_setting(f"last_run:{job}") != now.date().isoformat()

    def _mark(self, job: str) -> None:
        self.repo.set_setting(f"last_run:{job}", self._now_local().date().isoformat())

    def tick(self) -> list[str]:
        """Evaluate due jobs once; returns names queued (exposed for tests)."""
        fired: list[str] = []
        if not self.automation_enabled:
            return fired
        sc = self.cfg.get("schedule", {})
        if self._due("production", sc.get("production_time", "10:00")):
            self._mark("production")
            self.submit("daily pipeline", lambda: self._run_daily(), key="daily")
            fired.append("production")
        if self._due("analytics", sc.get("analytics_time", "08:00")) and self.s.collector_analytics:
            self._mark("analytics")
            self.submit("collect analytics", self.job_analytics, key="analytics")
            fired.append("analytics")
        if self._due("weekly_report", sc.get("analytics_time", "08:00"), sc.get("weekly_report_weekday", 0)):
            self._mark("weekly_report")
            self.submit("weekly report", lambda: self.s.reports.generate("weekly"), key="weekly")
            fired.append("weekly_report")
        return fired

    def _tick_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
                if self.s.publisher and int(time.time()) % 900 < int(self.cfg.get("schedule.tick_seconds", 30)):
                    self.submit("refresh upload status", self.s.publisher.refresh_statuses, key="refresh")
            except Exception:
                log.exception("scheduler tick failed")
            self._stop.wait(float(self.cfg.get("schedule.tick_seconds", 30)))

    # ---- job bodies -------------------------------------------------------------------
    def _run_daily(self) -> dict:
        pl = self._pipeline_factory()
        rid = pl.start()
        return pl.execute(rid)

    def job_analytics(self) -> dict:
        res = self.s.collector_analytics.collect_all()
        if res.get("collected"):
            self.s.optimizer.run()
        self.s.reports.generate("daily")
        return res

    def run_pipeline_now(self, topic_id=None, smoke=False, mode=None) -> int:
        pl = self._pipeline_factory()
        rid = pl.start(topic_id=topic_id, smoke=smoke, mode=mode)
        self.submit(f"pipeline run {rid}", lambda: pl.execute(rid), key=f"run:{rid}")
        return rid

    def resume_run(self, rid: int) -> bool:
        pl = self._pipeline_factory()
        return self.submit(f"resume run {rid}", lambda: pl.execute(rid), key=f"run:{rid}")
