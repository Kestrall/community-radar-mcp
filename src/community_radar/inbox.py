"""Inbox: threads pushed to the radar by an external collector (an n8n workflow, a cron job…) through POST /ingest.

The radar does not fetch these channels itself: whoever runs it decides what to send and stays responsible for
respecting each platform's terms. The inbox only validates, deduplicates, classifies and keeps the threads, so they
show up in every tool and in the report like any other channel.

Payload: {"source": "reddit", "channel": "r/ovh", "items": [{"id", "title", "url", "created", ...}]}
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import Thread
from .products import classify

SOURCE_RE = re.compile(r"[a-z][a-z0-9_-]{1,19}")
# Channels the radar collects itself: an import must not impersonate them.
RESERVED = {"forum", "github", "roadmap", "discord"}
MAX_ITEMS = 500
KEEP_DAYS = 120


def _date(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field}: ISO 8601 date expected")
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", value).strip()[:limit] if isinstance(value, str) else ""


def _int(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


class Inbox:
    """Imported threads, persisted to a JSON file when `path` is set (kept `KEEP_DAYS` days)."""

    def __init__(self, path: str | None = None) -> None:
        self.path = Path(path) if path else None
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        if self.path and self.path.exists():
            self._items = json.loads(self.path.read_text())

    @classmethod
    def from_env(cls) -> Inbox:
        return cls(os.getenv("RADAR_INBOX_PATH"))

    def ingest(self, payload: Any) -> dict[str, int]:
        """Validate and store a batch. Raises ValueError with a readable message on bad input."""
        if not isinstance(payload, dict):
            raise ValueError("JSON object expected")
        source, channel, items = payload.get("source"), _text(payload.get("channel"), 80), payload.get("items")
        if not isinstance(source, str) or not SOURCE_RE.fullmatch(source):
            raise ValueError("source: 2-20 lowercase letters, digits, - or _")
        if source in RESERVED:
            raise ValueError(f"source: {source!r} is collected by the radar itself")
        if not isinstance(items, list) or len(items) > MAX_ITEMS:
            raise ValueError(f"items: list of at most {MAX_ITEMS} threads expected")
        records = []
        for n, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"items[{n}]: object expected")
            ident, title, url = _text(item.get("id"), 120), _text(item.get("title"), 300), item.get("url")
            if not ident or not title or not isinstance(url, str) or not url.startswith("https://"):
                raise ValueError(f"items[{n}]: id, title and an https url are required")
            created = _date(item.get("created"), f"items[{n}].created")
            updated = _date(item["updated"], f"items[{n}].updated") if item.get("updated") else created
            records.append(
                {
                    "source": source,
                    "id": f"{source}:{ident}",
                    "title": title,
                    "url": url,
                    "created": created.isoformat(),
                    "last_activity": max(created, updated).isoformat(),
                    "excerpt": _text(item.get("text"), 300),
                    "author": _text(item.get("author"), 80),
                    "replies": _int(item.get("replies")),
                    "reactions": _int(item.get("score")),
                    "labels": [_text(lbl, 40) for lbl in item.get("labels", []) if isinstance(lbl, str)][:5],
                    "channel": channel or source,
                }
            )
        added = 0
        with self._lock:
            for record in records:
                added += record["id"] not in self._items
                self._items[record["id"]] = record
            self._expire()
            self._save()
        return {"received": len(records), "added": added, "kept": len(self._items)}

    def _expire(self) -> None:
        oldest = datetime.now(UTC) - timedelta(days=KEEP_DAYS)
        self._items = {k: v for k, v in self._items.items() if datetime.fromisoformat(v["last_activity"]) >= oldest}

    def _save(self) -> None:
        if self.path:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._items, ensure_ascii=False))
            tmp.replace(self.path)

    def channels(self) -> list[tuple[str, str]]:
        """Imported (source, channel) pairs, e.g. [("reddit", "r/ovh"), ("rss", "Cloud blog")]."""
        return sorted({(v["source"], v["channel"]) for v in self._items.values()})

    def threads(self) -> list[Thread]:
        """Imported threads as radar threads. Comment counts are often unknown, so `answered` stays None."""
        return [
            classify(
                Thread(
                    source=v["source"],
                    id=v["id"],
                    title=v["title"],
                    url=v["url"],
                    created=datetime.fromisoformat(v["created"]),
                    last_activity=datetime.fromisoformat(v["last_activity"]),
                    excerpt=v["excerpt"],
                    author=v["author"],
                    replies=v["replies"],
                    reactions=v["reactions"],
                    answered=(v["replies"] > 0) or None,
                    labels=list(v["labels"]),
                    channel=v["channel"],
                )
            )
            for v in self._items.values()
        ]
