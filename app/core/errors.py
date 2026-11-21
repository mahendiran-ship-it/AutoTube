"""Exception hierarchy. `retryable` drives the task manager's retry logic."""
from __future__ import annotations


class AutoTubeError(Exception):
    retryable = True


class NonRetryableError(AutoTubeError):
    retryable = False


class ConfigurationError(NonRetryableError):
    """Missing/invalid configuration or missing optional dependency."""


class ProviderUnavailable(AutoTubeError):
    """A provider (LLM, TTS, stock, ...) cannot serve this request right now."""


class BudgetExceeded(NonRetryableError):
    """A paid provider was blocked by the budget controller."""


class QuotaExceeded(NonRetryableError):
    """An external quota (e.g. YouTube API) is exhausted."""


class ValidationFailed(AutoTubeError):
    """Content failed validation (script, metadata, ...)."""

    def __init__(self, message: str, issues: list | None = None):
        super().__init__(message)
        self.issues = issues or []


class QualityCheckFailed(NonRetryableError):
    def __init__(self, message: str, report: dict | None = None):
        super().__init__(message)
        self.report = report or {}


class UnsafeURL(NonRetryableError):
    """URL rejected by the validator (SSRF / scheme / host rules)."""


class WaitingForApproval(Exception):
    """Control-flow signal: the pipeline paused for a human decision."""

    def __init__(self, run_id: int, stage: str, reason: str = ""):
        super().__init__(f"waiting for approval: {stage} ({reason})")
        self.run_id, self.stage, self.reason = run_id, stage, reason


class PipelinePaused(Exception):
    """Control-flow signal: the run was paused by the user."""


class StageFailed(NonRetryableError):
    def __init__(self, stage: str, message: str):
        super().__init__(f"stage '{stage}' failed: {message}")
        self.stage = stage


class RepairRequested(Exception):
    """Control-flow signal: QC asked the orchestrator to re-run media stages once."""
