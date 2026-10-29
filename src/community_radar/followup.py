"""Follow-ups for the community team: threads where the next move is ours, and what that move should be.

"Unanswered" only counts comments. A GitHub issue can be assigned, milestoned and linked to a fix while the reporter
still sees no human reply: the work is done, the communication is not. Reading the issue timeline tells those cases
apart from issues nobody looked at.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from .analytics import by_product, window
from .models import Thread
from .sources import STAFF_ASSOCIATIONS, Collector, Post, RateLimited, parse_date

# Most useful first: news to pass on, then silences, then conversations to resume.
STATUSES: dict[str, tuple[str, str]] = {
    "fixed_untold": (
        "fix merged, reporter not told",
        "Tell the reporter the fix is merged and which release ships it, then close the issue once released.",
    ),
    "silent_progress": (
        "in progress, silently",
        "Post a status update: thank the reporter, link the pull request and milestone, restate the workaround.",
    ),
    "closed_silently": (
        "closed without a word",
        "Explain the closure: fixed (link the pull request and release), duplicate (link it) or out of scope (why).",
    ),
    "no_response": (
        "no response",
        "Reply or triage: reproduce, label, answer or escalate to the product team.",
    ),
    "triaged_silently": (
        "labelled, no reply",
        "Acknowledge the report: confirm it is reproduced or ask for missing details, and say what happens next.",
    ),
    "waiting_on_us": (
        "waiting on us",
        "Follow up on the last message: the user answered and is waiting for the next step.",
    ),
    "community_answered": (
        "community answer only",
        "Check the community answer, confirm or correct it, and mark the solution.",
    ),
}
# Only maintainers can do these on someone else's issue.
MAINTAINER_EVENTS = ("labeled", "unlabeled", "assigned", "milestoned", "demilestoned", "closed", "issue_type_added")
# Silences only become a problem after a while; news should be passed on right away.
NEEDS_DELAY = ("no_response", "triaged_silently", "waiting_on_us", "community_answered")


@dataclass
class LinkedPR:
    number: int
    title: str
    url: str
    state: str  # open | merged | closed
    merged: datetime | None = None


@dataclass
class FollowUp:
    thread: Thread
    status: str
    reason: str
    since: datetime  # when the user last heard from a maintainer, or the event that calls for a message
    prs: list[LinkedPR] = field(default_factory=list)
    milestone: str | None = None
    assignees: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return STATUSES[self.status][0]

    @property
    def action(self) -> str:
        return STATUSES[self.status][1]

    def days_waiting(self, now: datetime | None = None) -> int:
        return ((now or datetime.now(UTC)) - self.since).days


def _days(delta_from: datetime, now: datetime) -> str:
    days = (now - delta_from).days
    return "today" if days == 0 else f"{days} day{'s' if days > 1 else ''} ago"


def _human(user: dict[str, Any] | None) -> bool:
    return bool(user) and user.get("type") != "Bot" and not user.get("login", "").endswith("[bot]")


def maintainers(events: list[dict[str, Any]], author: str) -> set[str]:
    """People who acted as maintainers on an issue. Those with a private organisation membership comment as
    "CONTRIBUTOR": they are recognised by what they do (label, assign, close someone else's issue)."""
    return {
        e["actor"]["login"]
        for e in events
        if e.get("event") in MAINTAINER_EVENTS and _human(e.get("actor")) and e["actor"].get("login") != author
    }


def assess_issue(thread: Thread, events: list[dict[str, Any]], now: datetime, min_days: int = 2) -> FollowUp | None:
    """What the community team should do next on a GitHub issue, from its timeline (None: nothing to do)."""
    if thread.staff_author:
        return None
    last_staff = last_user = touched = work = fixed = closed = None
    closed_by = ""
    milestone: str | None = None
    assignees: list[str] = []
    prs: dict[int, LinkedPR] = {}
    staff = maintainers(events, thread.author)
    for e in events:
        kind = e.get("event")
        at = parse_date(e.get("created_at"))
        actor = e.get("actor") or {}
        maintainer = _human(actor) and actor.get("login") != thread.author
        if kind == "commented" and _human(e.get("user")):
            if e.get("author_association") in STAFF_ASSOCIATIONS or e["user"].get("login") in staff:
                last_staff = at
            else:
                last_user = at
        elif kind in ("labeled", "issue_type_added") and maintainer:
            touched = at
        elif kind == "assigned" and maintainer:
            work = touched = at
            assignees.append((e.get("assignee") or {}).get("login", ""))
        elif kind == "unassigned":
            assignees = [a for a in assignees if a != (e.get("assignee") or {}).get("login")]
        elif kind == "milestoned" and maintainer:
            work = touched = at
            milestone = (e.get("milestone") or {}).get("title")
        elif kind == "demilestoned":
            milestone = None
        elif kind == "cross-referenced":
            source = (e.get("source") or {}).get("issue") or {}
            if source.get("pull_request"):
                merged = source["pull_request"].get("merged_at")
                pr = LinkedPR(
                    number=source["number"],
                    title=source.get("title", ""),
                    url=source.get("html_url", ""),
                    state="merged" if merged else source.get("state", "open"),
                    merged=parse_date(merged) if merged else None,
                )
                prs[pr.number] = pr
                if pr.merged:
                    fixed = max(fixed or pr.merged, pr.merged)
                elif pr.state == "open":
                    work = max(work or at, at)
        elif kind == "closed":
            closed, closed_by = at, actor.get("login", "")
            touched = at if maintainer else touched
            if e.get("commit_id"):
                fixed = at
        elif kind == "reopened":
            closed = None
    linked = sorted(prs.values(), key=lambda p: p.number)
    found = FollowUp(thread, "", "", last_staff or thread.created, linked, milestone, [a for a in assignees if a])
    heard = _days(last_staff, now) if last_staff else "ever"
    if closed and not thread.solved:
        closed = None  # timeline and issue state disagree: trust the issue
    if thread.solved:
        # Closed by a merged pull request: GitHub already shows "closed as completed in #n" to the reporter.
        if last_staff or fixed or not closed or not closed_by or closed_by == thread.author:
            return None
        found.status, found.since = "closed_silently", closed
        found.reason = f"Closed by {closed_by} {_days(closed, now)}, with no fix linked and no maintainer comment."
    elif fixed and (not last_staff or last_staff < fixed):
        found.status, found.since = "fixed_untold", fixed
        merged = [f"#{p.number}" for p in linked if p.state == "merged"]
        found.reason = (
            f"Fix merged {_days(fixed, now)}"
            + (f" (PR {', '.join(merged)})" if merged else " (closing commit)")
            + (f", milestone {milestone}" if milestone else "")
            + (f"; last maintainer comment {heard}." if last_staff else "; no maintainer comment ever.")
        )
    elif work and (not last_staff or last_staff < work):
        found.status = "silent_progress"
        evidence = [f"assigned to {', '.join(found.assignees)}"] if found.assignees else []
        evidence += [f"milestone {milestone}"] if milestone else []
        evidence += [f"PR #{p.number} open" for p in linked if p.state == "open"]
        found.reason = (
            f"Maintainers are on it ({', '.join(evidence) or 'recent activity'}) but the reporter was not told: "
            + (
                f"last maintainer comment {heard}."
                if last_staff
                else f"no maintainer comment since it was opened, {_days(thread.created, now)}."
            )
        )
    elif not last_staff:
        found.status = "triaged_silently" if touched else "no_response"
        others = f", {thread.replies} community comment{'s' if thread.replies > 1 else ''}" if thread.replies else ""
        found.reason = (
            f"Opened {_days(thread.created, now)}"
            + (" and labelled by maintainers, but never answered by a human" if touched else ", no maintainer activity")
            + f"{others}."
        )
    elif last_user and last_user > last_staff:
        found.status, found.since = "waiting_on_us", last_user
        found.reason = f"A user commented {_days(last_user, now)}, after the last maintainer answer ({heard})."
    else:
        return None
    if found.status in NEEDS_DELAY and found.days_waiting(now) < min_days:
        return None
    return found


def assess_forum(thread: Thread, posts: list[Post], now: datetime, min_days: int = 2) -> FollowUp | None:
    """What the community team should do next on a forum topic (None: nothing to do)."""
    if thread.solved or not posts:
        return None
    author = posts[0].author
    replies = [p for p in posts[1:] if p.author != author]
    staff = [p for p in posts[1:] if p.staff]
    if not replies:
        found = FollowUp(thread, "no_response", f"Opened {_days(thread.created, now)}, nobody replied.", thread.created)
    elif not staff:
        found = FollowUp(
            thread,
            "community_answered",
            f"{len(replies)} community repl{'ies' if len(replies) > 1 else 'y'}, no staff reply, no accepted solution.",
            thread.created,
        )
    elif not posts[-1].staff and posts[-1].created > staff[-1].created:
        last, answered = posts[-1], _days(staff[-1].created, now)
        found = FollowUp(
            thread,
            "waiting_on_us",
            f"{last.author} posted {_days(last.created, now)}, after the last staff answer ({answered}).",
            last.created,
        )
    else:
        return None
    if found.days_waiting(now) < min_days:
        return None
    return found


async def follow_ups(
    collector: Collector,
    threads: list[Thread],
    days: int,
    product: str | None = None,
    min_days: int = 2,
    max_checks: int = 60,
) -> list[FollowUp]:
    """Community threads active in the last `days` where the next move belongs to OVHcloud, most useful first.

    Reads one timeline per GitHub issue (cached): `max_checks` bounds the GitHub calls on large windows.
    """
    now = datetime.now(UTC)
    candidates = [
        t for t in by_product(window(threads, days), product) if t.source in ("github", "forum") and not t.staff_author
    ]
    candidates = sorted(candidates, key=lambda t: t.last_activity, reverse=True)[:max_checks]
    gate = asyncio.Semaphore(8)
    collector.errors.pop("timelines", None)

    async def check(t: Thread) -> FollowUp | None:
        async with gate:
            try:
                if t.source == "forum":
                    _, posts = await collector.forum_topic(t.id)
                    return assess_forum(t, posts, now, min_days)
                repo, number = t.id.rsplit("#", 1)
                return assess_issue(t, await collector.issue_timeline(repo, int(number)), now, min_days)
            except RateLimited as exc:
                collector.errors["timelines"] = str(exc)
            except (httpx.HTTPError, ValueError, KeyError):
                pass
            return None

    found = [f for f in await asyncio.gather(*(check(t) for t in candidates)) if f]
    order = list(STATUSES)
    return sorted(found, key=lambda f: (order.index(f.status), f.since))
