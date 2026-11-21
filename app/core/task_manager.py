"""Resumable task/stage tracking.

A *pipeline run* is a parent task; each stage is a child task persisted in SQLite.
Completed stages are never re-executed on resume; their stored result is returned.
"""
from __future__ import annotations

import time
import traceback
from typing import Any, Callable

from app.core import notifications as N
from app.core.errors import (NonRetryableError, PipelinePaused, RepairRequested, StageFailed, WaitingForApproval)
from app.core.logger import get_logger, redact
from app.database.database import utcnow
from app.database.models import TaskStatus as TS

log = get_logger("tasks")


class TaskManager:
    def __init__(self, repo, cfg=None, notifier=None, sleep: Callable[[float], None] = time.sleep):
        self.repo, self.cfg, self.notifier, self._sleep = repo, cfg, notifier, sleep

    # ---- runs ---------------------------------------------------------
    def start_run(self, name: str = "daily_pipeline", params: dict | None = None) -> int:
        run_id = self.repo.create_task("pipeline", name, status=TS.RUNNING.value, params=params)
        self.repo.update_task(run_id, started_at=utcnow())
        return run_id

    def set_run_status(self, run_id: int, status: TS, error: str | None = None) -> None:
        fields: dict[str, Any] = {"status": status.value}
        if status in (TS.COMPLETED, TS.FAILED):
            fields["ended_at"] = utcnow()
        if error is not None:
            fields["error"] = redact(error)[:2000]
        self.repo.update_task(run_id, **fields)

    def run_status(self, run_id: int) -> str:
        t = self.repo.get_task(run_id)
        return t["status"] if t else "unknown"

    def is_paused(self, run_id: int) -> bool:
        return self.run_status(run_id) == TS.PAUSED.value

    def pause_run(self, run_id: int) -> None:
        self.set_run_status(run_id, TS.PAUSED)

    def resume_run(self, run_id: int) -> None:
        self.set_run_status(run_id, TS.PENDING)
        self.repo.update_task(run_id, error=None)

    # ---- stages -------------------------------------------------------
    def stage_result(self, run_id: int, stage: str) -> dict | None:
        row = self.repo.find_stage(run_id, stage)
        if row and row["status"] == TS.COMPLETED.value:
            return row.get("result_json") or {}
        return None

    def run_stage(self, run_id: int, stage: str, fn: Callable[[], dict | None], *,
                  video_id: int | None = None, max_retries: int | None = None) -> dict:
        existing = self.repo.find_stage(run_id, stage)
        if existing and existing["status"] == TS.COMPLETED.value:
            log.info("stage %s already completed (run %s) — skipping", stage, run_id,
                     extra={"task_id": existing["id"], "stage": stage})
            return existing.get("result_json") or {}
        if self.is_paused(run_id):
            raise PipelinePaused()
        if max_retries is None:
            max_retries = int(self.cfg.get("pipeline.max_stage_retries", 2)) if self.cfg else 2
        backoff = float(self.cfg.get("pipeline.retry_backoff_seconds", 20)) if self.cfg else 0.0

        if existing:
            tid = existing["id"]
            self.repo.update_task(tid, status=TS.RUNNING.value, error=None, started_at=utcnow(), ended_at=None,
                                  max_retries=max_retries)
        else:
            tid = self.repo.create_task("stage", stage, stage=stage, parent_id=run_id, video_id=video_id,
                                        status=TS.RUNNING.value, max_retries=max_retries)
            self.repo.update_task(tid, started_at=utcnow())
        self.set_run_status(run_id, TS.RUNNING)

        attempt = 0
        while True:
            try:
                log.info("stage %s: running (attempt %d)", stage, attempt + 1, extra={"task_id": tid, "stage": stage})
                result = fn() or {}
                self.repo.update_task(tid, status=TS.COMPLETED.value, result_json=result, ended_at=utcnow(),
                                      error=None, retry_count=attempt)
                return result
            except WaitingForApproval:
                raise
            except RepairRequested:
                self.repo.update_task(tid, status=TS.PENDING.value, error="repair requested")
                raise
            except PipelinePaused:
                self.repo.update_task(tid, status=TS.PAUSED.value)
                raise
            except Exception as e:
                msg = redact(f"{type(e).__name__}: {e}")
                retryable = getattr(e, "retryable", True) and not isinstance(e, NonRetryableError)
                log.error("stage %s failed: %s", stage, msg, extra={"task_id": tid, "stage": stage},
                          exc_info=not isinstance(e, NonRetryableError))
                if retryable and attempt < max_retries:
                    attempt += 1
                    self.repo.update_task(tid, retry_count=attempt, error=msg[:2000])
                    self._sleep(backoff * attempt)
                    continue
                self.repo.update_task(tid, status=TS.FAILED.value, error=msg[:2000], ended_at=utcnow(),
                                      retry_count=attempt, result_json={"traceback": redact(traceback.format_exc())[-1500:]})
                self.set_run_status(run_id, TS.FAILED, error=f"{stage}: {msg}")
                if self.notifier:
                    self.notifier.notify(N.VIDEO_FAILED, f"Stage '{stage}' failed", msg, "error", video_id)
                raise StageFailed(stage, msg) from e

    def invalidate(self, run_id: int, stages: list[str]) -> None:
        """Mark stages as not-done so they run again on resume (e.g. their output files vanished)."""
        for st in stages:
            row = self.repo.find_stage(run_id, st)
            if row and row["status"] == TS.COMPLETED.value:
                self.repo.update_task(row["id"], status=TS.PENDING.value, result_json=None, ended_at=None,
                                      error="invalidated: output missing or repair requested")

    # ---- approvals ----------------------------------------------------
    def require_approval(self, run_id: int, stage: str, reason: str, video_id: int | None = None) -> None:
        """Create/keep an approval gate. Raises WaitingForApproval unless already approved."""
        row = self.repo.find_stage(run_id, stage)
        if row and row["status"] == TS.COMPLETED.value:
            return
        if row is None:
            tid = self.repo.create_task("stage", stage, stage=stage, parent_id=run_id, video_id=video_id,
                                        status=TS.WAITING.value, params={"reason": reason})
            self.repo.update_task(tid, started_at=utcnow())
            if self.notifier:
                self.notifier.notify(N.NEEDS_APPROVAL, "Approval needed", f"{reason} (run {run_id}, stage {stage})",
                                     "warning", video_id)
        else:
            self.repo.update_task(row["id"], status=TS.WAITING.value)
        self.set_run_status(run_id, TS.WAITING)
        raise WaitingForApproval(run_id, stage, reason)

    def approve(self, run_id: int, stage: str, approved: bool = True) -> bool:
        row = self.repo.find_stage(run_id, stage)
        if not row or row["status"] != TS.WAITING.value:
            return False
        if approved:
            self.repo.update_task(row["id"], status=TS.COMPLETED.value, ended_at=utcnow(),
                                  result_json={"approved": True, "at": utcnow()})
            self.set_run_status(run_id, TS.PENDING)
        else:
            self.repo.update_task(row["id"], status=TS.FAILED.value, ended_at=utcnow(),
                                  error="rejected by user", result_json={"approved": False})
            self.set_run_status(run_id, TS.FAILED, error=f"{stage} rejected by user")
        return True

    def waiting_approvals(self) -> list[dict]:
        return self.repo.list_tasks(kind="stage", status=TS.WAITING.value, limit=50)

    # ---- recovery -----------------------------------------------------
    def recover_interrupted(self) -> list[int]:
        """After a crash/restart: anything still 'running' was interrupted → mark resumable."""
        recovered = []
        for t in self.repo.list_tasks(status=TS.RUNNING.value, limit=500):
            if t["kind"] == "stage":
                self.repo.update_task(t["id"], status=TS.PENDING.value, error="interrupted (process stopped)")
            else:
                self.repo.update_task(t["id"], status=TS.PENDING.value, error="interrupted (process stopped)")
                recovered.append(t["id"])
        if recovered:
            log.warning("recovered %d interrupted pipeline run(s): %s", len(recovered), recovered)
        return recovered

    def resumable_runs(self) -> list[dict]:
        return [t for t in self.repo.list_tasks(kind="pipeline", status=TS.PENDING.value, limit=50)]
