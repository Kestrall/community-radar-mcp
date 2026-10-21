"""Community metrics and analyses: activity pulse, irritants, unanswered questions and content gaps."""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import median

from .docs import DocsIndex, tokens
from .models import Guide, Thread
from .products import NEGATIVE, label
from .sources import Collector

GAP_SCORE = 9.0  # below this BM25 score, no guide really covers the question


def window(threads: list[Thread], days: float, end: datetime | None = None, new_only: bool = False) -> list[Thread]:
    """Threads created (new_only) or active (new message, comment, vote…) during the `days` before `end`."""
    end = end or datetime.now(UTC)
    start = end - timedelta(days=days)
    return [t for t in threads if start <= (t.created if new_only else t.last_activity) < end]


def by_product(threads: list[Thread], product: str | None) -> list[Thread]:
    return [t for t in threads if product in t.products] if product else threads


@dataclass
class Pulse:
    days: int
    total: int
    by_source: dict[str, int]
    by_product: dict[str, int]
    by_signal: dict[str, int]
    unanswered: int
    answered_rate: float | None
    solved_rate: float | None
    median_first_response_hours: float | None
    staff_reply_rate: float | None
    new_threads: int
    previous_new_threads: int
    top_threads: list[Thread] = field(default_factory=list)


async def pulse(collector: Collector, threads: list[Thread], days: int, product: str | None) -> Pulse:
    current = by_product(window(threads, days), product)
    # Compare periods on threads created in them: activity-based windows would empty the previous period, since
    # anything active then and active again since only counts in the current one.
    new = by_product(window(threads, days, new_only=True), product)
    previous = by_product(window(threads, days, datetime.now(UTC) - timedelta(days=days), new_only=True), product)
    # Roadmap items are wishes, not questions: they do not count in answer metrics.
    known = [t for t in current if t.answered is not None and t.source != "roadmap"]
    # First response time only makes sense for topics opened in the period, not for old topics revived by a reply.
    forum = [t for t in new if t.source == "forum"]
    responses = await asyncio.gather(*(collector.first_response(t) for t in forum))
    delays = [
        (first - t.created).total_seconds() / 3600 for t, (first, _) in zip(forum, responses, strict=True) if first
    ]
    answered_forum = [staff for t, (first, staff) in zip(forum, responses, strict=True) if first]
    questions = [t for t in current if t.source in ("forum", "github")]
    return Pulse(
        days=days,
        total=len(current),
        by_source=dict(Counter(t.source for t in current).most_common()),
        by_product={label(p): n for p, n in Counter(p for t in current for p in t.products).most_common()},
        by_signal=dict(Counter(s for t in current for s in t.signals).most_common()),
        unanswered=sum(1 for t in known if not t.answered and not t.solved),
        answered_rate=round(sum(1 for t in known if t.answered) / len(known), 2) if known else None,
        solved_rate=round(sum(1 for t in questions if t.solved) / len(questions), 2) if questions else None,
        median_first_response_hours=round(median(delays), 1) if delays else None,
        staff_reply_rate=round(sum(answered_forum) / len(answered_forum), 2) if answered_forum else None,
        new_threads=len(new),
        previous_new_threads=len(previous),
        top_threads=sorted(current, key=lambda t: -t.engagement)[:5],
    )


def unanswered(threads: list[Thread], days: int, product: str | None) -> list[Thread]:
    """Open questions nobody answered yet, oldest and most viewed first (they hurt the most)."""
    now = datetime.now(UTC)
    pending = [
        t
        for t in by_product(window(threads, days), product)
        if t.source in ("forum", "github") and t.answered is False and not t.solved
    ]
    return sorted(pending, key=lambda t: -((now - t.created).total_seconds() / 3600 * (1 + t.views / 50)))


@dataclass
class Irritant:
    product: str
    label: str
    threads: int
    new_threads: int
    previous_threads: int
    negative_threads: int
    signals: dict[str, int]
    engagement: float
    score: float
    examples: list[Thread]


def irritants(threads: list[Thread], days: int, limit: int = 6) -> list[Irritant]:
    """Products that generate the most negative, engaging or fast-growing conversations."""
    current = window(threads, days)
    created = Counter(p for t in window(threads, days, new_only=True) for p in t.products or ["other"])
    previous = window(threads, days, datetime.now(UTC) - timedelta(days=days), new_only=True)
    groups: dict[str, list[Thread]] = defaultdict(list)
    for t in current:
        for p in t.products or ["other"]:
            groups[p].append(t)
    before = Counter(p for t in previous for p in t.products or ["other"])
    results = []
    for product, items in groups.items():
        negative = [t for t in items if set(t.signals) & set(NEGATIVE)]
        engagement = sum(t.engagement for t in items)
        growth = max(0, created[product] - before[product]) / max(before[product], 1)
        # Roadmap items are wishes rather than pain points: they weigh half as much.
        volume = sum(0.5 if t.source == "roadmap" else 1 for t in items)
        score = len(negative) * 3 + volume + engagement / 10 + growth * 2
        results.append(
            Irritant(
                product=product,
                label=label(product) if product != "other" else "Other / unclassified",
                threads=len(items),
                new_threads=created[product],
                previous_threads=before[product],
                negative_threads=len(negative),
                signals=dict(Counter(s for t in items for s in t.signals).most_common()),
                engagement=round(engagement, 1),
                score=round(score, 1),
                examples=sorted(items, key=lambda t: (-(t in negative), -t.engagement))[:4],
            )
        )
    results.sort(key=lambda i: -i.score)
    return results[:limit]


@dataclass
class Gap:
    thread: Thread
    best_guide: Guide | None
    best_score: float


def content_gaps(
    threads: list[Thread], docs: DocsIndex, days: int, limit: int = 10, min_score: float = GAP_SCORE
) -> list[Gap]:
    """Questions asked by the community that no official guide clearly answers: tutorial candidates."""
    questions = [
        t
        for t in window(threads, days)
        if t.source in ("forum", "reddit")
        or (t.source == "github" and "question" in t.signals and not set(t.signals) & {"bug", "incident", "security"})
    ]
    gaps = []
    for t in questions:
        hits = docs.search(t.title, limit=1)
        guide, score = hits[0] if hits else (None, 0.0)
        if score < min_score:
            gaps.append(Gap(t, guide, score))
    gaps.sort(key=lambda g: (-g.thread.engagement, g.best_score))
    return gaps[:limit]


def search_threads(threads: list[Thread], query: str, by_votes: bool = False) -> list[Thread]:
    """Threads matching most query words; ties broken by engagement (or by community votes)."""
    terms = set(tokens(query))
    if not terms:
        return []
    scored = []
    for t in threads:
        title, body = set(tokens(t.title)), set(tokens(t.text))
        ratio = (len(terms & body) + len(terms & title)) / (2 * len(terms))  # title matches count double
        if terms & body:
            scored.append((ratio, t))
    best = max((s for s, _ in scored), default=0)
    scored = [(s, t) for s, t in scored if s >= max(0.5, best * 0.75)]
    scored.sort(key=lambda x: (-x[0], -(x[1].reactions if by_votes else x[1].engagement)))
    return [t for _, t in scored]
