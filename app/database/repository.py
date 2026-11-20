"""Data access layer. All SQL lives here (parameterised; table/column names validated)."""
from __future__ import annotations

import json
from typing import Any

from app.database.database import Database, utcnow

_JSON_COLS = {
    "signal_data", "score_json", "keywords", "categories", "qc_json", "metadata_json", "render_info_json",
    "notes_json", "sources_json", "sections_json", "pronunciation_json", "captions_json", "validation_json",
    "factcheck_json", "meta_json", "response_json", "retention_json", "raw_json", "result_json",
    "params_json", "extra_json", "change_json", "evidence_json",
}


def _dumps(v: Any) -> Any:
    return json.dumps(v, ensure_ascii=False, default=str) if v is not None else None


def _row(r) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in list(d):
        if k in _JSON_COLS and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except (ValueError, TypeError):
                pass
    return d


def _rows(rs) -> list[dict]:
    return [_row(r) for r in rs]


class Repository:
    def __init__(self, db: Database):
        self.db = db
        self._cols: dict[str, set[str]] = {}

    # ---- generic helpers ---------------------------------------------
    def _columns(self, table: str) -> set[str]:
        if table not in self._cols:
            cols = set(self.db.columns(table))
            if not cols:
                raise ValueError(f"unknown table {table}")
            self._cols[table] = cols
        return self._cols[table]

    def insert(self, table: str, **fields) -> int:
        cols = self._columns(table)
        data = {k: (_dumps(v) if (k in _JSON_COLS and not isinstance(v, str)) else v)
                for k, v in fields.items() if k in cols}
        unknown = set(fields) - cols
        if unknown:
            raise ValueError(f"unknown columns for {table}: {sorted(unknown)}")
        names = ",".join(data)
        marks = ",".join("?" for _ in data)
        cur = self.db.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", list(data.values()))
        return int(cur.lastrowid)

    def update(self, table: str, row_id: int, **fields) -> None:
        if not fields:
            return
        cols = self._columns(table)
        unknown = set(fields) - cols
        if unknown:
            raise ValueError(f"unknown columns for {table}: {sorted(unknown)}")
        data = {k: (_dumps(v) if (k in _JSON_COLS and not isinstance(v, str)) else v) for k, v in fields.items()}
        sets = ",".join(f"{k}=?" for k in data)
        self.db.execute(f"UPDATE {table} SET {sets} WHERE id=?", [*data.values(), row_id])

    def get(self, table: str, row_id: int) -> dict | None:
        self._columns(table)
        return _row(self.db.one(f"SELECT * FROM {table} WHERE id=?", (row_id,)))

    # ---- settings -----------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        r = self.db.one("SELECT value FROM settings WHERE key=?", (key,))
        if r is None:
            return default
        try:
            return json.loads(r["value"])
        except (ValueError, TypeError):
            return r["value"]

    def set_setting(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), utcnow()))

    # ---- sources ------------------------------------------------------
    def upsert_source(self, source_key: str, name: str, url: str, reliability: str = "medium",
                      enabled: bool = True, kind: str = "rss", categories: list | None = None) -> int:
        r = self.db.one("SELECT id FROM sources WHERE source_key=?", (source_key,))
        if r:
            return int(r["id"])
        return self.insert("sources", source_key=source_key, name=name, url=url, reliability=reliability,
                           enabled=1 if enabled else 0, kind=kind, categories=categories or [], created_at=utcnow())

    def list_sources(self) -> list[dict]:
        return _rows(self.db.query("SELECT * FROM sources ORDER BY name"))

    def record_source_fetch(self, source_id: int, ok: bool, items: int = 0, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE sources SET last_fetch_at=?, last_status=?, last_error=?, items_last_fetch=? WHERE id=?",
            (utcnow(), "ok" if ok else "error", error, items, source_id))

    # ---- topics -------------------------------------------------------
    def find_topic_by_fingerprint(self, fp: str) -> dict | None:
        return _row(self.db.one("SELECT * FROM topics WHERE fingerprint=?", (fp,)))

    def add_topic(self, **fields) -> int | None:
        """Insert a topic; returns None when the fingerprint already exists."""
        fields.setdefault("discovered_at", utcnow())
        fields.setdefault("status", "candidate")
        if fields.get("fingerprint") and self.find_topic_by_fingerprint(fields["fingerprint"]):
            return None
        return self.insert("topics", **fields)

    def list_topics(self, status: str | None = None, limit: int = 200) -> list[dict]:
        if status:
            rs = self.db.query("SELECT * FROM topics WHERE status=? ORDER BY COALESCE(score_total,0) DESC, id DESC LIMIT ?",
                               (status, limit))
        else:
            rs = self.db.query("SELECT * FROM topics ORDER BY id DESC LIMIT ?", (limit,))
        return _rows(rs)

    def topics_for_dedup(self) -> list[dict]:
        """Topics that count as 'already covered or in progress'."""
        return _rows(self.db.query(
            "SELECT id,title,normalized_title,keywords,status,summary FROM topics "
            "WHERE status IN ('selected','published')"))

    def recent_candidates(self, days: int = 21) -> list[dict]:
        return _rows(self.db.query(
            "SELECT * FROM topics WHERE status='candidate' AND "
            "datetime(discovered_at) >= datetime('now', ?) ORDER BY id DESC", (f"-{int(days)} days",)))

    def set_topic_status(self, topic_id: int, status: str, **extra) -> None:
        self.update("topics", topic_id, status=status, **extra)

    # ---- videos -------------------------------------------------------
    def create_video(self, topic_id: int | None, title: str, publish_mode: str, run_id: int | None = None,
                     fmt: str = "short", is_smoke: bool = False) -> int:
        now = utcnow()
        return self.insert("videos", topic_id=topic_id, title=title, format=fmt, status="planned",
                           publish_mode=publish_mode, run_id=run_id, is_smoke=1 if is_smoke else 0,
                           created_at=now, updated_at=now)

    def update_video(self, video_id: int, **fields) -> None:
        fields["updated_at"] = utcnow()
        self.update("videos", video_id, **fields)

    def get_video(self, video_id: int) -> dict | None:
        return self.get("videos", video_id)

    def list_videos(self, status: str | None = None, limit: int = 100) -> list[dict]:
        if status:
            return _rows(self.db.query("SELECT * FROM videos WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)))
        return _rows(self.db.query("SELECT * FROM videos ORDER BY id DESC LIMIT ?", (limit,)))

    def video_by_hash(self, content_hash: str, exclude_id: int | None = None) -> dict | None:
        q = "SELECT * FROM videos WHERE content_hash=?"
        p: list = [content_hash]
        if exclude_id is not None:
            q += " AND id<>?"
            p.append(exclude_id)
        return _row(self.db.one(q + " LIMIT 1", p))

    # ---- research / scripts / scenes ---------------------------------
    def save_research(self, video_id: int, topic_id: int | None, summary: str, notes: dict,
                      sources: list, method: str) -> int:
        self.db.execute("DELETE FROM research WHERE video_id=?", (video_id,))
        return self.insert("research", video_id=video_id, topic_id=topic_id, summary=summary,
                           notes_json=notes, sources_json=sources, method=method, created_at=utcnow())

    def get_research(self, video_id: int) -> dict | None:
        return _row(self.db.one("SELECT * FROM research WHERE video_id=? ORDER BY id DESC LIMIT 1", (video_id,)))

    def save_script(self, video_id: int, **fields) -> int:
        r = self.db.one("SELECT MAX(version) AS v FROM scripts WHERE video_id=?", (video_id,))
        version = int(r["v"] or 0) + 1
        self.db.execute("UPDATE scripts SET is_active=0 WHERE video_id=?", (video_id,))
        return self.insert("scripts", video_id=video_id, version=version, is_active=1, created_at=utcnow(), **fields)

    def get_script(self, video_id: int) -> dict | None:
        return _row(self.db.one("SELECT * FROM scripts WHERE video_id=? AND is_active=1 ORDER BY id DESC LIMIT 1",
                                (video_id,)))

    def replace_scenes(self, video_id: int, script_id: int | None, scenes: list[dict]) -> list[int]:
        self.db.execute("DELETE FROM scenes WHERE video_id=?", (video_id,))
        ids = []
        for s in scenes:
            ids.append(self.insert("scenes", video_id=video_id, script_id=script_id, **s))
        return ids

    def list_scenes(self, video_id: int) -> list[dict]:
        return _rows(self.db.query("SELECT * FROM scenes WHERE video_id=? ORDER BY idx", (video_id,)))

    def update_scene(self, scene_id: int, **fields) -> None:
        self.update("scenes", scene_id, **fields)

    # ---- assets / licenses -------------------------------------------
    def add_license(self, **fields) -> int:
        return self.insert("licenses", created_at=utcnow(), **fields)

    def add_asset(self, **fields) -> int:
        return self.insert("assets", created_at=utcnow(), **fields)

    def list_assets(self, video_id: int) -> list[dict]:
        return _rows(self.db.query("SELECT * FROM assets WHERE video_id=? ORDER BY id", (video_id,)))

    def delete_assets(self, video_id: int, methods: tuple[str, ...] | None = None) -> None:
        if methods:
            marks = ",".join("?" for _ in methods)
            self.db.execute(f"DELETE FROM assets WHERE video_id=? AND method IN ({marks})", (video_id, *methods))
        else:
            self.db.execute("DELETE FROM assets WHERE video_id=?", (video_id,))

    def get_license(self, license_id: int) -> dict | None:
        return self.get("licenses", license_id)

    def asset_source_used(self, source_url: str, exclude_video: int | None = None) -> bool:
        q = "SELECT 1 FROM assets WHERE source_url=?"
        p: list = [source_url]
        if exclude_video is not None:
            q += " AND video_id<>?"
            p.append(exclude_video)
        return self.db.one(q + " LIMIT 1", p) is not None

    def music_usage(self) -> dict[str, int]:
        rs = self.db.query("SELECT path, COUNT(*) AS n FROM assets WHERE method='music' GROUP BY path")
        return {r["path"]: int(r["n"]) for r in rs}

    # ---- uploads ------------------------------------------------------
    def create_upload(self, video_id: int, privacy: str, publish_at: str | None) -> int:
        now = utcnow()
        return self.insert("uploads", video_id=video_id, status="uploading", privacy=privacy,
                           publish_at=publish_at, attempts=1, created_at=now, updated_at=now)

    def update_upload(self, upload_id: int, **fields) -> None:
        fields["updated_at"] = utcnow()
        self.update("uploads", upload_id, **fields)

    def uploads_for_video(self, video_id: int) -> list[dict]:
        return _rows(self.db.query("SELECT * FROM uploads WHERE video_id=? ORDER BY id DESC", (video_id,)))

    def successful_upload(self, video_id: int) -> dict | None:
        return _rows(self.db.query(
            "SELECT * FROM uploads WHERE video_id=? AND youtube_video_id IS NOT NULL "
            "AND status IN ('uploaded','scheduled','published') ORDER BY id DESC LIMIT 1", (video_id,)) or [None])[0]

    def upload_by_youtube_id(self, yt_id: str) -> dict | None:
        return _row(self.db.one("SELECT * FROM uploads WHERE youtube_video_id=?", (yt_id,)))

    def list_uploads(self, limit: int = 100) -> list[dict]:
        return _rows(self.db.query("SELECT * FROM uploads ORDER BY id DESC LIMIT ?", (limit,)))

    def taken_publish_slots(self) -> list[str]:
        rs = self.db.query("SELECT publish_at FROM uploads WHERE publish_at IS NOT NULL "
                           "AND status IN ('uploading','uploaded','scheduled','published')")
        return [r["publish_at"] for r in rs]

    # ---- analytics ----------------------------------------------------
    def upsert_analytics(self, video_id: int | None, youtube_video_id: str, snapshot_date: str, **metrics) -> None:
        data = {"video_id": video_id, "youtube_video_id": youtube_video_id, "snapshot_date": snapshot_date,
                "created_at": utcnow(), **metrics}
        cols = self._columns("analytics")
        data = {k: (_dumps(v) if (k in _JSON_COLS and not isinstance(v, str)) else v)
                for k, v in data.items() if k in cols}
        names = ",".join(data)
        marks = ",".join("?" for _ in data)
        upd = ",".join(f"{k}=excluded.{k}" for k in data if k not in ("youtube_video_id", "snapshot_date"))
        self.db.execute(f"INSERT INTO analytics ({names}) VALUES ({marks}) "
                        f"ON CONFLICT(youtube_video_id, snapshot_date) DO UPDATE SET {upd}", list(data.values()))

    def latest_analytics(self) -> list[dict]:
        rs = self.db.query(
            "SELECT a.* FROM analytics a JOIN (SELECT youtube_video_id, MAX(snapshot_date) d FROM analytics "
            "GROUP BY youtube_video_id) m ON a.youtube_video_id=m.youtube_video_id AND a.snapshot_date=m.d")
        return _rows(rs)

    def analytics_history(self, youtube_video_id: str) -> list[dict]:
        return _rows(self.db.query("SELECT * FROM analytics WHERE youtube_video_id=? ORDER BY snapshot_date",
                                   (youtube_video_id,)))

    def save_channel_stats(self, snapshot_date: str, **fields) -> None:
        cols = self._columns("channel_stats")
        data = {"snapshot_date": snapshot_date, "created_at": utcnow(), **fields}
        data = {k: (_dumps(v) if (k in _JSON_COLS and not isinstance(v, str)) else v) for k, v in data.items() if k in cols}
        names = ",".join(data)
        marks = ",".join("?" for _ in data)
        upd = ",".join(f"{k}=excluded.{k}" for k in data if k != "snapshot_date")
        self.db.execute(f"INSERT INTO channel_stats ({names}) VALUES ({marks}) "
                        f"ON CONFLICT(snapshot_date) DO UPDATE SET {upd}", list(data.values()))

    def channel_stats_history(self, limit: int = 90) -> list[dict]:
        return _rows(self.db.query("SELECT * FROM channel_stats ORDER BY snapshot_date DESC LIMIT ?", (limit,)))

    # ---- tasks --------------------------------------------------------
    def create_task(self, kind: str, name: str, stage: str | None = None, parent_id: int | None = None,
                    video_id: int | None = None, status: str = "pending", max_retries: int = 0,
                    params: dict | None = None) -> int:
        now = utcnow()
        return self.insert("tasks", kind=kind, name=name, stage=stage, parent_id=parent_id, video_id=video_id,
                           status=status, max_retries=max_retries, params_json=params, created_at=now, updated_at=now)

    def update_task(self, task_id: int, **fields) -> None:
        fields["updated_at"] = utcnow()
        self.update("tasks", task_id, **fields)

    def get_task(self, task_id: int) -> dict | None:
        return self.get("tasks", task_id)

    def find_stage(self, run_id: int, stage: str) -> dict | None:
        return _row(self.db.one("SELECT * FROM tasks WHERE parent_id=? AND stage=? ORDER BY id DESC LIMIT 1",
                                (run_id, stage)))

    def list_stages(self, run_id: int) -> list[dict]:
        return _rows(self.db.query("SELECT * FROM tasks WHERE parent_id=? ORDER BY id", (run_id,)))

    def list_tasks(self, kind: str | None = None, status: str | None = None, limit: int = 100) -> list[dict]:
        q, p = "SELECT * FROM tasks WHERE 1=1", []
        if kind:
            q += " AND kind=?"
            p.append(kind)
        if status:
            q += " AND status=?"
            p.append(status)
        return _rows(self.db.query(q + " ORDER BY id DESC LIMIT ?", [*p, limit]))

    # ---- logs / usage / strategy / notifications ----------------------
    def add_log(self, level: str, logger: str, message: str, task_id: int | None = None,
                category: str | None = None, extra: dict | None = None) -> None:
        self.db.execute("INSERT INTO logs(ts,level,logger,message,task_id,category,extra_json) VALUES (?,?,?,?,?,?,?)",
                        (utcnow(), level, logger, message, task_id, category, _dumps(extra)))

    def list_logs(self, level: str | None = None, category: str | None = None, limit: int = 200) -> list[dict]:
        q, p = "SELECT * FROM logs WHERE 1=1", []
        if level:
            q += " AND level=?"
            p.append(level.upper())
        if category:
            q += " AND category=?"
            p.append(category)
        return _rows(self.db.query(q + " ORDER BY id DESC LIMIT ?", [*p, limit]))

    def add_api_usage(self, provider: str, kind: str, ok: bool, model: str | None = None,
                      prompt_tokens: int | None = None, completion_tokens: int | None = None,
                      cost_inr: float = 0.0, cost_known: bool = True, error: str | None = None,
                      video_id: int | None = None) -> int:
        return self.insert("api_usage", ts=utcnow(), provider=provider, kind=kind, model=model,
                           prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, cost_inr=cost_inr,
                           cost_known=1 if cost_known else 0, ok=1 if ok else 0, error=error, video_id=video_id)

    def api_usage_summary(self, since_days: int = 30) -> list[dict]:
        return _rows(self.db.query(
            "SELECT provider, kind, COUNT(*) AS calls, SUM(ok) AS ok_calls, SUM(cost_inr) AS cost_inr, "
            "SUM(COALESCE(prompt_tokens,0)) AS prompt_tokens, SUM(COALESCE(completion_tokens,0)) AS completion_tokens "
            "FROM api_usage WHERE datetime(ts) >= datetime('now', ?) GROUP BY provider, kind",
            (f"-{int(since_days)} days",)))

    def month_cost_inr(self) -> float:
        r = self.db.one("SELECT COALESCE(SUM(cost_inr),0) AS c FROM api_usage WHERE strftime('%Y-%m', ts)=strftime('%Y-%m','now')")
        return float(r["c"] or 0)

    def add_strategy_change(self, kind: str, change: dict, reason: str, evidence: dict | None = None,
                            auto_applied: bool = False, status: str = "proposed") -> int:
        return self.insert("strategy_changes", ts=utcnow(), kind=kind, change_json=change, reason=reason,
                           evidence_json=evidence, auto_applied=1 if auto_applied else 0, status=status)

    def list_strategy_changes(self, limit: int = 50) -> list[dict]:
        return _rows(self.db.query("SELECT * FROM strategy_changes ORDER BY id DESC LIMIT ?", (limit,)))

    def add_notification(self, level: str, event: str, title: str, message: str, video_id: int | None = None) -> int:
        return self.insert("notifications", ts=utcnow(), level=level, event=event, title=title,
                           message=message, video_id=video_id)

    def list_notifications(self, limit: int = 50, unread_only: bool = False) -> list[dict]:
        q = "SELECT * FROM notifications " + ("WHERE read=0 " if unread_only else "") + "ORDER BY id DESC LIMIT ?"
        return _rows(self.db.query(q, (limit,)))

    def mark_notifications_read(self) -> None:
        self.db.execute("UPDATE notifications SET read=1 WHERE read=0")
