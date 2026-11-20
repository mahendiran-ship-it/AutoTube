"""Phase 1: configuration, database, task manager, logging redaction, validators."""
from __future__ import annotations

import pytest

from app.core.config import Config, deep_merge, load_dotenv
from app.core.errors import ConfigurationError, NonRetryableError, StageFailed, UnsafeURL, WaitingForApproval
from app.core.logger import redact
from app.database.database import Database
from app.database.models import MIGRATIONS
from app.utils.validators import sanitize_filename, validate_url


# ---------------- configuration ----------------
def test_config_loads_channel_and_defaults(cfg):
    assert cfg.get("channel.name") == "Cosmic Unknown"
    assert cfg.get("channel.timezone") == "Asia/Kolkata"
    assert cfg.get("schedule.publish_time") == "18:30"
    assert cfg.publishing_mode == "REVIEW"            # safe default
    assert cfg.get("budget.monthly_limit_inr") == 0
    assert str(cfg.tz) == "Asia/Kolkata"
    assert cfg.validate() == []


def test_config_validation_catches_errors(cfg):
    cfg.set("pipeline.publishing_mode", "YOLO")
    cfg.set("schedule.publish_time", "25:99")
    errs = cfg.validate()
    assert any("publishing_mode" in e for e in errs)
    assert any("publish_time" in e for e in errs)


def test_local_override_roundtrip_and_secret_rejection(cfg):
    cfg.save_local_override({"channel": {"name": "My Channel"}})
    reloaded = Config.load(config_dir=cfg.config_dir, root=cfg.root, load_env=False)
    assert (cfg.config_dir / "local.yaml").exists()
    assert cfg.get("channel.name") == "My Channel"
    assert reloaded.get("channel.name") == "My Channel"
    with pytest.raises(ConfigurationError):
        cfg.save_local_override({"youtube": {"api_key": "AIzaSECRET"}})
    # *_env keys (names of env vars, not secrets) are allowed
    cfg.save_local_override({"youtube": {"client_secrets_env": "MY_VAR"}})


def test_public_dict_hides_secret_values(cfg):
    cfg.set("something.api_key", "hunter2-hunter2")
    assert "hunter2" not in str(cfg.public_dict())


def test_deep_merge_and_dotenv(tmp_path, monkeypatch):
    assert deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"b": 9}}) == {"a": {"b": 9, "c": 2}}
    env = tmp_path / ".env"
    env.write_text('FOO_TEST_X="bar"\n# c\nEMPTY=\nKEEP_TEST_Y=1\n')
    monkeypatch.delenv("FOO_TEST_X", raising=False)
    monkeypatch.setenv("KEEP_TEST_Y", "orig")
    assert load_dotenv(env) == 1
    import os
    assert os.environ["FOO_TEST_X"] == "bar" and os.environ["KEEP_TEST_Y"] == "orig"


# ---------------- database ----------------
def test_database_creation_and_migrations_are_idempotent(cfg):
    d = Database(cfg.db_path)
    applied = d.migrate()
    assert applied == [v for v, _, _ in MIGRATIONS]
    assert d.migrate() == []                       # second run: nothing to do
    tables = set(d.table_names())
    for t in ("topics", "research", "scripts", "scenes", "assets", "videos", "uploads", "analytics",
              "tasks", "logs", "settings", "sources", "licenses", "api_usage", "schema_migrations"):
        assert t in tables, t
    assert d.current_version() == MIGRATIONS[-1][0]


def test_foreign_keys_enforced(repo):
    with pytest.raises(Exception):
        repo.insert("scripts", video_id=9999, narration="x")


def test_repo_rejects_unknown_columns(repo):
    with pytest.raises(ValueError):
        repo.insert("topics", title="x", **{"evil; DROP TABLE topics": 1})


def test_settings_roundtrip(repo):
    repo.set_setting("k", {"a": [1, 2]})
    assert repo.get_setting("k") == {"a": [1, 2]}
    assert repo.get_setting("missing", 5) == 5


# ---------------- task manager ----------------
def test_stage_runs_once_and_resumes_without_rerun(tm):
    run = tm.start_run()
    calls = []
    assert tm.run_stage(run, "a", lambda: calls.append(1) or {"x": 1}) == {"x": 1}
    assert tm.run_stage(run, "a", lambda: calls.append(2) or {"x": 2}) == {"x": 1}   # cached
    assert calls == [1]


def test_stage_retries_then_succeeds(tm, repo):
    run = tm.start_run()
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] < 3:
            raise RuntimeError("transient")
        return {"ok": True}
    assert tm.run_stage(run, "flaky", flaky, max_retries=2) == {"ok": True}
    row = repo.find_stage(run, "flaky")
    assert row["status"] == "completed" and row["retry_count"] == 2


def test_stage_non_retryable_fails_immediately_and_notifies(tm, repo):
    run = tm.start_run()
    n = {"c": 0}

    def bad():
        n["c"] += 1
        raise NonRetryableError("nope")
    with pytest.raises(StageFailed):
        tm.run_stage(run, "bad", bad, max_retries=5)
    assert n["c"] == 1
    assert tm.run_status(run) == "failed"
    assert repo.list_notifications()[0]["event"] == "video_failed"


def test_retry_limit_is_bounded(tm):
    run = tm.start_run()
    n = {"c": 0}

    def always():
        n["c"] += 1
        raise RuntimeError("x")
    with pytest.raises(StageFailed):
        tm.run_stage(run, "s", always, max_retries=2)
    assert n["c"] == 3          # 1 try + 2 retries, never endless


def test_approval_gate_and_resume(tm):
    run = tm.start_run()
    with pytest.raises(WaitingForApproval):
        tm.require_approval(run, "upload_approval", "review please")
    assert tm.run_status(run) == "waiting_for_approval"
    assert tm.approve(run, "upload_approval") is True
    tm.require_approval(run, "upload_approval", "review please")    # now passes silently
    assert tm.approve(run, "upload_approval") is False              # not waiting any more


def test_recover_interrupted_after_restart(tm, repo):
    run = tm.start_run()
    sid = repo.create_task("stage", "voice", stage="voice", parent_id=run, status="running")
    recovered = tm.recover_interrupted()
    assert run in recovered
    assert repo.get_task(sid)["status"] == "pending"
    assert tm.resumable_runs()[0]["id"] == run


def test_paused_run_does_not_execute_stages(tm):
    from app.core.errors import PipelinePaused
    run = tm.start_run()
    tm.pause_run(run)
    with pytest.raises(PipelinePaused):
        tm.run_stage(run, "x", lambda: {})


# ---------------- security helpers ----------------
def test_redaction_masks_secrets(monkeypatch):
    monkeypatch.setenv("MY_SERVICE_TOKEN", "supersecretvalue123")
    out = redact("token=supersecretvalue123 and Authorization: Bearer abcdefghijklmnop api_key=AIzaSyA1234567890123456789012")
    assert "supersecretvalue123" not in out
    assert "abcdefghijklmnop" not in out
    assert "AIzaSyA12345" not in out


@pytest.mark.parametrize("bad", ["../../etc/passwd", "a/b\\c.mp4", "CON", "<script>.png", "", "...."])
def test_sanitize_filename(bad):
    s = sanitize_filename(bad)
    assert "/" not in s and "\\" not in s and ".." not in s and s


@pytest.mark.parametrize("url", [
    "http://example.com/x", "https://127.0.0.1/x", "https://localhost/x", "https://10.0.0.5/a",
    "https://user:pw@example.com/", "file:///etc/passwd", "https://169.254.169.254/latest/meta-data",
    "ftp://example.com/x"])
def test_validate_url_rejects_unsafe(url):
    with pytest.raises(UnsafeURL):
        validate_url(url, check_dns=False)


def test_validate_url_allowlist_and_dns():
    assert validate_url("https://videos.pexels.com/a.mp4", allowed_hosts=["pexels.com"], check_dns=False)
    with pytest.raises(UnsafeURL):
        validate_url("https://evil.com/a.mp4", allowed_hosts=["pexels.com"], check_dns=False)
    with pytest.raises(UnsafeURL):   # public-looking name that resolves to a private IP
        validate_url("https://rebind.example/x", resolver=lambda h: ["192.168.1.10"])
    assert validate_url("https://good.example/x", resolver=lambda h: ["93.184.216.34"])
