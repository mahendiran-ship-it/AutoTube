"""Dependency container: builds every service from a Config. Used by the CLI, scheduler, dashboard and tests."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.ai.provider_manager import ProviderManager
from app.analytics.optimizer import PerformanceOptimizer
from app.analytics.reports import ReportGenerator
from app.content.content_validator import ContentValidator
from app.content.scene_planner import ScenePlanner
from app.content.script_writer import ScriptWriter
from app.content.seo_generator import SEOGenerator
from app.core.budget import BudgetController
from app.core.config import Config
from app.core.logger import attach_db, setup_logging
from app.core.notifications import Notifier
from app.core.resource_monitor import ResourceMonitor
from app.core.task_manager import TaskManager
from app.database.database import Database
from app.database.repository import Repository
from app.editing.audio_mixer import AudioMixer
from app.editing.thumbnail_generator import ThumbnailGenerator
from app.editing.video_editor import VideoEditor
from app.generation.visual_manager import VisualManager
from app.generation.voice_generator import VoiceGenerator
from app.quality.qc_engine import QCEngine
from app.research.fact_checker import FactChecker
from app.research.feed_collector import FeedCollector
from app.research.research_engine import ResearchEngine
from app.research.topic_discovery import TopicDiscovery, WikipediaTrends
from app.research.topic_selector import DuplicateDetector, TopicScorer, TopicSelector


@dataclass
class Services:
    cfg: Config
    db: Database
    repo: Repository
    notifier: Notifier
    tm: TaskManager
    budget: BudgetController
    resources: ResourceMonitor
    ai: ProviderManager
    collector: FeedCollector
    discovery: TopicDiscovery
    dedup: DuplicateDetector
    selector: TopicSelector
    research: ResearchEngine
    fact_checker: FactChecker
    validator: ContentValidator
    writer: ScriptWriter
    planner: ScenePlanner
    seo: SEOGenerator
    voice: VoiceGenerator
    visuals: VisualManager
    mixer: AudioMixer
    editor: VideoEditor
    thumbs: ThumbnailGenerator
    qc: QCEngine
    optimizer: PerformanceOptimizer
    reports: ReportGenerator
    auth: Any = None
    publisher: Any = None
    collector_analytics: Any = None
    extras: dict = field(default_factory=dict)


def build_services(cfg: Config, *, setup_log: bool = True) -> Services:
    if setup_log:
        setup_logging(cfg)
    db = Database(cfg.db_path)
    db.migrate()
    repo = Repository(db)
    if setup_log:
        attach_db(repo)
    notifier = Notifier(repo, cfg)
    tm = TaskManager(repo, cfg, notifier)
    budget = BudgetController(cfg, repo)
    resources = ResourceMonitor(cfg)
    ai = ProviderManager(cfg, repo, budget, notifier)
    collector = FeedCollector(cfg, repo)
    trends = WikipediaTrends(cfg)
    discovery = TopicDiscovery(cfg, repo, collector, trends)
    dedup = DuplicateDetector(repo, cfg)
    selector = TopicSelector(cfg, repo, TopicScorer(cfg, ai), dedup, notifier)
    research = ResearchEngine(cfg, repo, ai, feed_collector=collector)
    fact_checker = FactChecker(cfg, ai)
    validator = ContentValidator(cfg)
    s = Services(
        cfg=cfg, db=db, repo=repo, notifier=notifier, tm=tm, budget=budget, resources=resources, ai=ai,
        collector=collector, discovery=discovery, dedup=dedup, selector=selector, research=research,
        fact_checker=fact_checker, validator=validator, writer=ScriptWriter(cfg, ai, validator, fact_checker),
        planner=ScenePlanner(cfg, ai), seo=SEOGenerator(cfg, ai), voice=VoiceGenerator(cfg, repo),
        visuals=VisualManager(cfg, repo, budget, resources=resources), mixer=AudioMixer(cfg, repo),
        editor=VideoEditor(cfg, resources), thumbs=ThumbnailGenerator(cfg), qc=QCEngine(cfg, repo),
        optimizer=PerformanceOptimizer(cfg, repo, ai), reports=ReportGenerator(cfg, repo))
    try:
        from app.publishing.youtube_auth import YouTubeAuth
        from app.publishing.uploader import Publisher
        from app.analytics.collector import AnalyticsCollector
        s.auth = YouTubeAuth(cfg)
        s.publisher = Publisher(cfg, repo, s.auth, notifier)
        s.collector_analytics = AnalyticsCollector(cfg, repo, s.auth, notifier)
    except ImportError:
        pass
    return s
