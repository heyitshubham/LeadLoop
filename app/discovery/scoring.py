"""Deterministic intent scoring. Every point comes with a human-readable reason,
so the board can show *why* a lead ranks where it does."""

import re
from datetime import datetime, timezone

from app.models import Signal

SOURCE_BASE = {"reddit": 45, "jobs": 40}
STRONG_PHRASES = [
    "need a list",
    "looking for a dataset",
    "need data",
    "lead generation",
    "market research",
    "data entry",
    "web scraping",
    "competitor",
    "database of",
    "prospect list",
    "prospecting",
    "inside sales",
    "outbound",
]
FUNDING = re.compile(r"\b(raise[sd]?|secures?|bags?|lands?|closes?|nets?|funding|seed|series [a-d])\b", re.I)
UNITS = {"minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30}


def days_ago(posted: str | None) -> float | None:
    """Understands "5 hours ago", "12 days ago", "2 weeks ago" and ISO timestamps."""
    if not posted:
        return None
    m = re.search(r"(\d+)\+?\s*(minute|hour|day|week|month)", posted.lower())
    if m:
        return int(m.group(1)) * UNITS[m.group(2)]
    try:
        when = datetime.fromisoformat(posted.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:  # some news dates carry no timezone; treat them as UTC
        when = when.replace(tzinfo=timezone.utc)
    return max((datetime.now(timezone.utc) - when).total_seconds() / 86400, 0)


def _recency_points(posted: str | None) -> tuple[int, str | None]:
    age = days_ago(posted)
    if age is None:
        return 0, None
    label = "today" if age < 1 else f"{round(age)} day{'s' * (round(age) != 1)} ago"
    if age <= 1:
        return 15, f"posted {label}"
    if age <= 7:
        return 10, f"posted {label}"
    if age <= 14:
        return 5, f"posted {label}"
    return 0, None


def score_signal(sig: Signal) -> tuple[int, list[str]]:
    reasons = [f"{sig.source}: {sig.title}"]

    if sig.source == "news":
        # Fresh funding = budget to spend on growth, which is what our data is for.
        age = days_ago(sig.posted)
        if FUNDING.search(sig.title) and (age is None or age <= 30):
            score = 40
            reasons.append("raised funding recently" if age is None else f"raised funding {round(age)} days ago")
        else:
            score = 15
        pts, _ = _recency_points(sig.posted)
        return min(score + pts, 100), reasons

    score = SOURCE_BASE.get(sig.source, 20)
    text = f"{sig.title} {sig.snippet}".lower()
    hits = [p for p in STRONG_PHRASES if p in text]
    if hits:
        score += min(10 * len(hits), 30)
        reasons.append("asks for: " + ", ".join(hits))
    else:
        score //= 2  # being on a source isn't intent; it can only corroborate a stronger signal

    pts, why = _recency_points(sig.posted)
    score += pts
    if why:
        reasons.append(why)

    return min(score, 100), reasons


def score_lead(signals: list[Signal]) -> tuple[int, list[str]]:
    """A lead's score is its best signal, plus a bonus per *different* source that agrees.
    (Ten postings from one company are one signal, not ten.)"""
    scored = sorted((score_signal(s) for s in signals), key=lambda x: -x[0])
    best, reasons = scored[0]
    sources = {s.source for s in signals}
    if len(sources) > 1:
        best = min(best + 10 * (len(sources) - 1), 100)
        reasons = reasons + [f"confirmed by {len(sources)} sources: {', '.join(sorted(sources))}"]
    elif len(signals) > 1:
        reasons = reasons + [f"{len(signals)} postings"]
    return best, reasons
