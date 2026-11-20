"""Shared fixtures. Tests never touch the network or paid APIs."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import Config  # noqa: E402
from app.core.notifications import Notifier  # noqa: E402
from app.core.task_manager import TaskManager  # noqa: E402
from app.database.database import Database  # noqa: E402
from app.database.repository import Repository  # noqa: E402


@pytest.fixture
def cfg(tmp_path) -> Config:
    c = Config.load(config_dir=ROOT / "config", root=tmp_path, load_env=False,
                    overrides={"pipeline": {"retry_backoff_seconds": 0},
                               "voice": {"provider_order": ["test_tone"]}})
    # isolate local overrides written by tests
    (tmp_path / "config").mkdir(exist_ok=True)
    c.config_dir = tmp_path / "config"
    return c


@pytest.fixture
def db(cfg) -> Database:
    d = Database(cfg.db_path)
    d.migrate()
    yield d
    d.close()


@pytest.fixture
def repo(db) -> Repository:
    return Repository(db)


@pytest.fixture
def notifier(repo, cfg) -> Notifier:
    return Notifier(repo, cfg, adapters=[])


@pytest.fixture
def tm(repo, cfg, notifier) -> TaskManager:
    return TaskManager(repo, cfg, notifier, sleep=lambda s: None)


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                  reason="ffmpeg not installed")
