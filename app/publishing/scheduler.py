"""Publish-slot calculation in the channel timezone (default Asia/Kolkata). DST-safe via zoneinfo."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone


def parse_hhmm(s: str) -> time:
    h, m = str(s).split(":")
    return time(int(h), int(m))


def to_rfc3339_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_rfc3339(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def next_publish_slot(cfg, taken: list[str], now: datetime | None = None) -> datetime:
    """First future slot at schedule.publish_time (channel tz) on a day with spare capacity.

    `taken` = RFC3339 timestamps of already-scheduled/published uploads. Capacity per day = schedule.videos_per_day.
    The slot must be at least schedule.publish_min_lead_minutes in the future (YouTube needs a future time)."""
    tz = cfg.tz
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    lead = timedelta(minutes=int(cfg.get("schedule.publish_min_lead_minutes", 20)))
    per_day = max(1, int(cfg.get("schedule.videos_per_day", 1)))
    at = parse_hhmm(cfg.get("schedule.publish_time", "18:30"))
    per_day_count: dict[date, int] = {}
    for t in taken:
        try:
            d = parse_rfc3339(t).astimezone(tz).date()
            per_day_count[d] = per_day_count.get(d, 0) + 1
        except ValueError:
            continue
    day = now.astimezone(tz).date()
    for _ in range(366):
        slot = datetime.combine(day, at, tzinfo=tz)
        if slot.astimezone(timezone.utc) >= now + lead and per_day_count.get(day, 0) < per_day:
            return slot
        day += timedelta(days=1)
    raise RuntimeError("no free publish slot in the next year")


def describe_slot(dt: datetime) -> str:
    return dt.strftime("%a %d %b %Y, %H:%M %Z")
