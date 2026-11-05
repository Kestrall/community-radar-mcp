"""Triage: the community team's queue. Every thread the radar sees gets a status, an owner team and a history.

The radar keeps watching threads after they are handled: when the community writes again on a thread marked
answered, waiting on the user, escalated or resolved, the thread comes back to the top of the queue flagged
"new activity". Nothing is ever posted on the platforms: statuses live in the radar only.
"""

from __future__ import annotations

import sqlite3
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .models import Thread

# key: (label, meaning). Order = default queue order once "new activity" threads are on top.
STATUSES: dict[str, tuple[str, str]] = {
    "new": ("New", "not triaged yet"),
    "in_progress": ("In progress", "someone is on it"),
    "escalated": ("Escalated", "handed to an internal team, waiting for its answer"),
    "waiting_user": ("Waiting on user", "we asked the user for details"),
    "answered": ("Answered", "we replied, not confirmed solved"),
    "resolved": ("Resolved", "solved, nothing left to do"),
    "dismissed": ("Dismissed", "duplicate, off-topic or spam"),
}
TEAMS = {"support": "Support", "product": "Product", "engineering": "Engineering", "docs": "Docs"}
# Statuses where the next move belongs to the community team.
OPEN = ("new", "in_progress")
# Statuses where a new message from the community deserves a look.
WATCHED = ("escalated", "waiting_user", "answered", "resolved", "dismissed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    url TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL,
    team TEXT, assignee TEXT, note TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL, at TEXT NOT NULL, actor TEXT NOT NULL,
    from_status TEXT NOT NULL, to_status TEXT NOT NULL, team TEXT, assignee TEXT, note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_url ON events (url, id);
"""


@dataclass
class Ticket:
    url: str
    source: str
    title: str
    status: str
    team: str | None
    assignee: str | None
    note: str
    updated: datetime


@dataclass
class Event:
    at: datetime
    actor: str
    from_status: str
    to_status: str
    team: str | None
    assignee: str | None
    note: str


@dataclass
class Item:
    """A thread as it stands in the queue."""

    thread: Thread
    status: str = "new"
    team: str | None = None
    assignee: str | None = None
    note: str = ""
    updated: datetime | None = None
    new_activity: bool = False  # the community wrote after our last status change
    hint: str = ""  # what the radar's follow-up analysis suggests
    hint_status: str = ""

    @property
    def label(self) -> str:
        return STATUSES[self.status][0]

    @property
    def needs_action(self) -> bool:
        """In the team's "to handle" view: new community activity, or an open thread that is the community team's
        job. Roadmap wishes belong to product teams and issues opened by maintainers are internal work, unless the
        radar flags something to do on them."""
        if self.new_activity:
            return True
        if self.status not in OPEN:
            return False
        return bool(self.hint) or (self.thread.source != "roadmap" and not self.thread.staff_author)


@dataclass
class Queue:
    items: list[Item]
    counts: dict[str, int] = field(default_factory=dict)
    new_activity: int = 0
    to_handle: int = 0


def _clean(value: str | None, limit: int) -> str | None:
    value = (value or "").strip()
    return value[:limit] or None


class TriageStore:
    """Statuses and history, in SQLite (a file when `path` is set, memory otherwise)."""

    def __init__(self, path: str | None = None) -> None:
        self._db = sqlite3.connect(path or ":memory:", check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def tickets(self) -> dict[str, Ticket]:
        with self._lock:
            rows = self._db.execute(
                "SELECT url, source, title, status, team, assignee, note, updated FROM tickets"
            ).fetchall()
        return {r[0]: Ticket(*r[:7], datetime.fromisoformat(r[7])) for r in rows}

    def update(
        self,
        thread: Thread,
        status: str,
        note: str = "",
        team: str | None = None,
        assignee: str | None = None,
        actor: str = "",
    ) -> Ticket:
        """Change a thread's status (or just add a note) and log it. Raises ValueError on bad input."""
        if status not in STATUSES:
            raise ValueError(f"status must be one of: {', '.join(STATUSES)}")
        team = _clean(team, 20)
        if team and team not in TEAMS:
            raise ValueError(f"team must be one of: {', '.join(TEAMS)}")
        if status == "escalated" and not team:
            raise ValueError("escalated needs a team: " + ", ".join(TEAMS))
        if status == "dismissed" and not (note or "").strip():
            raise ValueError("dismissed needs a note saying why (duplicate, off-topic, spam…)")
        note = _clean(note, 2000) or ""
        assignee = _clean(assignee, 60)
        actor = _clean(actor, 60) or "unknown"
        now = datetime.now(UTC)
        with self._lock:
            previous = self._db.execute("SELECT status FROM tickets WHERE url = ?", (thread.url,)).fetchone()
            self._db.execute(
                "INSERT INTO events (url, at, actor, from_status, to_status, team, assignee, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (thread.url, now.isoformat(), actor, previous[0] if previous else "new", status, team, assignee, note),
            )
            self._db.execute(
                "INSERT INTO tickets (url, source, title, status, team, assignee, note, updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(url) DO UPDATE SET source = excluded.source, "
                "title = excluded.title, status = excluded.status, team = excluded.team, "
                "assignee = excluded.assignee, note = excluded.note, updated = excluded.updated",
                (thread.url, thread.source, thread.title[:300], status, team, assignee, note, now.isoformat()),
            )
            self._db.commit()
        return Ticket(thread.url, thread.source, thread.title[:300], status, team, assignee, note, now)

    def history(self, url: str) -> list[Event]:
        with self._lock:
            rows = self._db.execute(
                "SELECT at, actor, from_status, to_status, team, assignee, note FROM events WHERE url = ? ORDER BY id",
                (url,),
            ).fetchall()
        return [Event(datetime.fromisoformat(r[0]), *r[1:]) for r in rows]


def build_queue(threads: list[Thread], store: TriageStore, hints: dict[str, tuple[str, str]] | None = None) -> Queue:
    """Threads with their triage state, most urgent first: new community activity on handled threads, then threads
    whose next move is ours (radar hint first), then the rest by status."""
    tickets = store.tickets()
    hints = hints or {}
    items = []
    for t in threads:
        ticket = tickets.get(t.url)
        item = Item(t)
        if ticket:
            item.status, item.team, item.assignee = ticket.status, ticket.team, ticket.assignee
            item.note, item.updated = ticket.note, ticket.updated
            item.new_activity = ticket.status in WATCHED and t.last_activity > ticket.updated
        item.hint_status, item.hint = hints.get(t.url, ("", ""))
        items.append(item)
    order = list(STATUSES)
    items.sort(
        key=lambda i: (
            not i.new_activity,
            not i.needs_action,
            order.index(i.status),
            not i.hint,
            -i.thread.last_activity.timestamp(),
        )
    )
    return Queue(
        items,
        dict(Counter(i.status for i in items)),
        sum(i.new_activity for i in items),
        sum(i.needs_action for i in items),
    )
