"""Budget controller. Default: ₹0 — paid providers are blocked, never silently used."""
from __future__ import annotations

from app.core.errors import BudgetExceeded
from app.core.logger import get_logger

log = get_logger("budget")


class BudgetController:
    def __init__(self, cfg, repo):
        self.cfg, self.repo = cfg, repo

    @property
    def limit_inr(self) -> float:
        return float(self.cfg.get("budget.monthly_limit_inr", 0) or 0)

    @property
    def paid_allowed(self) -> bool:
        return bool(self.cfg.get("budget.allow_paid_providers", False)) and self.limit_inr > 0

    def spent_inr(self) -> float:
        return self.repo.month_cost_inr()

    def authorize(self, provider: str, is_paid: bool, estimated_cost_inr: float = 0.0) -> None:
        """Raise BudgetExceeded when the call must not happen."""
        if not is_paid:
            return
        if not self.paid_allowed:
            raise BudgetExceeded(
                f"Provider '{provider}' is marked paid but the budget is ₹{self.limit_inr:g} "
                f"(allow_paid_providers={self.cfg.get('budget.allow_paid_providers', False)}). Blocked.")
        if self.spent_inr() + estimated_cost_inr > self.limit_inr:
            raise BudgetExceeded(
                f"Monthly budget ₹{self.limit_inr:g} would be exceeded (spent ₹{self.spent_inr():.2f}).")

    def summary(self) -> dict:
        usage = self.repo.api_usage_summary(30)
        unknown = any(u.get("cost_inr") is None for u in usage)
        return {
            "monthly_limit_inr": self.limit_inr,
            "spent_this_month_inr": round(self.spent_inr(), 2),
            "estimated_cost_per_video_inr": 0.0 if not self.paid_allowed else None,
            "paid_providers_allowed": self.paid_allowed,
            "cost_tracking_note": ("Only costs reported by providers are tracked; "
                                   + self.cfg.get("budget.note", "")),
            "usage_30d": usage,
            "has_unknown_costs": unknown,
        }
