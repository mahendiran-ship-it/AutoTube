"""Schema (versioned migrations) and status enums."""
from __future__ import annotations

from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    WAITING = "waiting_for_approval"


class VideoStatus(str, Enum):
    PLANNED = "planned"
    RESEARCHED = "researched"
    SCRIPTED = "scripted"
    SCENES_PLANNED = "scenes_planned"
    VOICED = "voiced"
    VISUALS_READY = "visuals_ready"
    RENDERED = "rendered"
    METADATA_READY = "metadata_ready"
    QC_PASSED = "qc_passed"
    QC_FAILED = "qc_failed"
    AWAITING_APPROVAL = "awaiting_approval"
    UPLOADING = "uploading"
    UPLOADED_PRIVATE = "uploaded_private"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    FAILED = "failed"
    REJECTED = "rejected"


class PublishMode(str, Enum):
    MANUAL = "MANUAL"
    REVIEW = "REVIEW"
    PRIVATE = "PRIVATE"
    AUTO = "AUTO"


# Order used for "has this video reached stage X" comparisons.
VIDEO_PROGRESS = [s.value for s in (
    VideoStatus.PLANNED, VideoStatus.RESEARCHED, VideoStatus.SCRIPTED, VideoStatus.SCENES_PLANNED,
    VideoStatus.VOICED, VideoStatus.VISUALS_READY, VideoStatus.RENDERED, VideoStatus.METADATA_READY,
    VideoStatus.QC_PASSED)]

MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "initial_schema", """
CREATE TABLE sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key TEXT NOT NULL UNIQUE,
    name TEXT, kind TEXT DEFAULT 'rss', url TEXT, reliability TEXT DEFAULT 'medium',
    enabled INTEGER DEFAULT 1, categories TEXT,
    last_fetch_at TEXT, last_status TEXT, last_error TEXT, items_last_fetch INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE TABLE topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    normalized_title TEXT,
    fingerprint TEXT UNIQUE,
    summary TEXT, url TEXT,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    source_name TEXT,
    signal_type TEXT DEFAULT 'research_signal',   -- 'research_signal' | 'trend' (measured only)
    signal_data TEXT,
    category TEXT, keywords TEXT,
    published_at TEXT, discovered_at TEXT,
    status TEXT DEFAULT 'candidate',              -- candidate|selected|rejected|published|skipped
    score_total REAL, score_json TEXT, score_method TEXT,
    selection_reason TEXT, rejection_reason TEXT, selected_at TEXT
);
CREATE INDEX idx_topics_status ON topics(status);
CREATE INDEX idx_topics_discovered ON topics(discovered_at);
CREATE TABLE videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER REFERENCES topics(id) ON DELETE SET NULL,
    title TEXT, format TEXT DEFAULT 'short',
    status TEXT, publish_mode TEXT,
    final_path TEXT, thumbnail_path TEXT, srt_path TEXT,
    duration_seconds REAL, width INTEGER, height INTEGER,
    content_hash TEXT,
    quality_score REAL, qc_json TEXT, metadata_json TEXT, render_info_json TEXT,
    run_id INTEGER, is_smoke INTEGER DEFAULT 0,
    created_at TEXT, updated_at TEXT
);
CREATE INDEX idx_videos_status ON videos(status);
CREATE INDEX idx_videos_hash ON videos(content_hash);
CREATE TABLE research (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER REFERENCES videos(id) ON DELETE CASCADE,
    topic_id INTEGER REFERENCES topics(id) ON DELETE SET NULL,
    summary TEXT, notes_json TEXT, sources_json TEXT, method TEXT, created_at TEXT
);
CREATE TABLE scripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    version INTEGER DEFAULT 1,
    narration TEXT, sections_json TEXT,
    estimated_duration REAL, pronunciation_json TEXT, captions_json TEXT,
    generated_by TEXT, provider TEXT,
    validation_json TEXT, factcheck_json TEXT,
    is_active INTEGER DEFAULT 1, created_at TEXT
);
CREATE TABLE licenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT, license_name TEXT, license_url TEXT,
    author TEXT, author_url TEXT, attribution_text TEXT,
    attribution_required INTEGER DEFAULT 0,
    source_url TEXT, notes TEXT, created_at TEXT
);
CREATE TABLE assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER REFERENCES videos(id) ON DELETE CASCADE,
    scene_id INTEGER,
    kind TEXT,                                   -- video|image|audio|thumbnail|subtitle|clip
    method TEXT,                                 -- stock_video|stock_image|ai_image|ai_video|procedural|image_motion|tts|music|sfx|ambient
    provider TEXT, path TEXT, source_url TEXT,
    license_id INTEGER REFERENCES licenses(id) ON DELETE SET NULL,
    meta_json TEXT, created_at TEXT
);
CREATE INDEX idx_assets_video ON assets(video_id);
CREATE INDEX idx_assets_source ON assets(source_url);
CREATE TABLE scenes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    script_id INTEGER REFERENCES scripts(id) ON DELETE SET NULL,
    idx INTEGER NOT NULL,
    section TEXT, narration TEXT,
    est_duration REAL, duration REAL,
    visual_description TEXT, visual_prompt TEXT, visual_kind TEXT, search_query TEXT,
    camera TEXT, lighting TEXT, atmosphere TEXT, sfx TEXT, transition TEXT,
    audio_path TEXT, audio_duration REAL, start_time REAL,
    asset_id INTEGER REFERENCES assets(id) ON DELETE SET NULL,
    visual_method TEXT,
    UNIQUE(video_id, idx)
);
CREATE TABLE uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    youtube_video_id TEXT, url TEXT,
    status TEXT,                                 -- uploading|uploaded|scheduled|published|failed
    privacy TEXT, publish_at TEXT,
    attempts INTEGER DEFAULT 0, error TEXT,
    thumbnail_status TEXT, schedule_verified TEXT, response_json TEXT,
    created_at TEXT, updated_at TEXT
);
CREATE UNIQUE INDEX idx_uploads_ytid ON uploads(youtube_video_id) WHERE youtube_video_id IS NOT NULL;
CREATE TABLE analytics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER REFERENCES videos(id) ON DELETE CASCADE,
    youtube_video_id TEXT, snapshot_date TEXT,
    views INTEGER, watch_minutes REAL, avg_view_duration REAL, avg_view_percentage REAL,
    likes INTEGER, comments INTEGER, subs_gained INTEGER, subs_lost INTEGER,
    impressions INTEGER, ctr REAL,
    revenue REAL, revenue_status TEXT,          -- reported | unavailable
    retention_json TEXT, raw_json TEXT, created_at TEXT,
    UNIQUE(youtube_video_id, snapshot_date)
);
CREATE TABLE channel_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date TEXT UNIQUE,
    subscribers INTEGER, total_views INTEGER, video_count INTEGER,
    watch_minutes_28d REAL, watch_hours_365d REAL,
    raw_json TEXT, created_at TEXT
);
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    video_id INTEGER,
    kind TEXT,                                   -- pipeline | stage | job
    name TEXT, stage TEXT,
    status TEXT, retry_count INTEGER DEFAULT 0, max_retries INTEGER DEFAULT 0,
    error TEXT, result_json TEXT, params_json TEXT,
    created_at TEXT, started_at TEXT, ended_at TEXT, updated_at TEXT
);
CREATE INDEX idx_tasks_parent ON tasks(parent_id);
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE TABLE logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, level TEXT, logger TEXT, message TEXT,
    task_id INTEGER, category TEXT, extra_json TEXT
);
CREATE INDEX idx_logs_ts ON logs(ts);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
CREATE TABLE api_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, provider TEXT, kind TEXT, model TEXT,
    prompt_tokens INTEGER, completion_tokens INTEGER,
    cost_inr REAL DEFAULT 0, cost_known INTEGER DEFAULT 1,
    ok INTEGER, error TEXT, video_id INTEGER
);
CREATE TABLE strategy_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, kind TEXT, change_json TEXT, reason TEXT, evidence_json TEXT,
    auto_applied INTEGER DEFAULT 0,
    status TEXT DEFAULT 'proposed'               -- proposed | applied | dismissed
);
CREATE TABLE notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, level TEXT, event TEXT, title TEXT, message TEXT,
    read INTEGER DEFAULT 0, video_id INTEGER
);
"""),
]
