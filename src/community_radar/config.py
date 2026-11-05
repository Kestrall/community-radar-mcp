"""Watched sources, editable from the Sources page: profile, forums, roadmap, GitHub repositories, Discord servers,
subreddits and feeds.

Secrets never live here: GITHUB_TOKEN, DISCORD_BOT_TOKEN, Reddit credentials and RADAR_INGEST_TOKEN stay in the
environment. Without a saved configuration, the defaults come from the environment: RADAR_PROFILE picks a profile
whose preset sources are loaded (nothing for the neutral default), DISCORD_GUILD_ID adds a Discord server.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

LIMIT = 20  # per kind: keeps GitHub and Discord calls reasonable

REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
GUILD_RE = re.compile(r"\d{15,21}")
SUBREDDIT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_]{1,20}")
CHANNELS_RE = re.compile(r"[\w#, -]{0,300}")
CATEGORY_RE = re.compile(r"[\w./-]{0,100}")
KINDS = ("forums", "github", "discord", "reddit", "feeds")


def _flag(value: Any, default: bool = True) -> bool:
    return value if isinstance(value, bool) else default


def _text(value: Any, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


@dataclass
class SourceConfig:
    profile: str = "auto"  # "auto" (detected from the sources) or a key of profiles.PROFILES
    roadmap: str = ""  # owner/repo whose issues are the public roadmap (optional)
    forums: list[dict[str, Any]] = field(default_factory=list)  # Discourse: {"url", "category", "label", "enabled"}
    github: list[dict[str, Any]] = field(default_factory=list)  # {"repo", "enabled"}
    discord: list[dict[str, Any]] = field(default_factory=list)  # {"guild", "label", "channels", "demo", "enabled"}
    reddit: list[dict[str, Any]] = field(default_factory=list)  # {"subreddit", "enabled"}
    feeds: list[dict[str, Any]] = field(default_factory=list)  # {"url", "label", "enabled"}

    @classmethod
    def defaults(cls) -> SourceConfig:
        guild = os.getenv("DISCORD_GUILD_ID", "")
        discord = (
            [
                {
                    "guild": guild,
                    "label": "",
                    "channels": os.getenv("DISCORD_CHANNELS", ""),
                    "demo": os.getenv("DISCORD_DEMO", "").lower() in ("1", "true", "yes"),
                    "enabled": True,
                }
            ]
            if guild
            else []
        )
        from .profiles import PROFILES

        profile = os.getenv("RADAR_PROFILE", "auto")
        preset = PROFILES[profile].preset if profile in PROFILES else {}
        return cls.parse({**preset, "profile": profile if profile in PROFILES else "auto", "discord": discord})

    @classmethod
    def parse(cls, data: Any) -> SourceConfig:
        """Validate a configuration sent by the Sources page. Raises ValueError with a readable message."""
        if not isinstance(data, dict):
            raise ValueError("JSON object expected")
        from .profiles import PROFILES, detect

        profile = _text(data.get("profile"), 20) or "auto"
        if profile != "auto" and profile not in PROFILES:
            raise ValueError(f"profile must be auto or one of: {', '.join(PROFILES)}")
        roadmap = _text(data.get("roadmap"), 140).removeprefix("https://github.com/").strip("/")
        if roadmap and not REPO_RE.fullmatch(roadmap):
            raise ValueError(f"roadmap: {roadmap!r} is not an owner/repo name")
        config = cls(profile=profile, roadmap=roadmap)
        for kind in KINDS:
            items = data.get(kind, [])
            if not isinstance(items, list) or len(items) > LIMIT:
                raise ValueError(f"{kind}: list of at most {LIMIT} entries expected")
            seen: set[str] = set()
            for n, item in enumerate(items):
                if not isinstance(item, dict):
                    raise ValueError(f"{kind}[{n}]: object expected")
                entry = getattr(cls, f"_{kind}")(item, f"{kind} #{n + 1}")
                key = next(iter(entry.values())).lower()
                if key in seen:
                    raise ValueError(f"{kind} #{n + 1}: {key} is listed twice")
                seen.add(key)
                getattr(config, kind).append(entry)
        if "forums" not in data:  # saved before forums were configurable: they were the detected profile's own
            config.forums = [cls._forums(f, "preset forum") for f in detect(config).preset.get("forums", [])]
        return config

    @staticmethod
    def _forums(item: dict[str, Any], where: str) -> dict[str, Any]:
        url = _text(item.get("url"), 200).rstrip("/")
        category = _text(item.get("category"), 100).strip("/").removeprefix("c/")
        if not url.startswith("https://") or any(c.isspace() for c in url) or url.count("/") < 2:
            raise ValueError(f"{where}: the forum address starts with https:// (a Discourse forum)")
        if not CATEGORY_RE.fullmatch(category):
            raise ValueError(f"{where}: the category is a slug/id like public-cloud/11, or empty for the whole forum")
        return {
            "url": url,
            "category": category,
            "label": _text(item.get("label"), 60) or url.removeprefix("https://").split("/")[0],
            "enabled": _flag(item.get("enabled")),
        }

    @staticmethod
    def _github(item: dict[str, Any], where: str) -> dict[str, Any]:
        repo = _text(item.get("repo"), 140).removeprefix("https://github.com/").strip("/")
        if not REPO_RE.fullmatch(repo):
            raise ValueError(f"{where}: {repo or 'empty'!r} is not an owner/repo name")
        return {"repo": repo, "enabled": _flag(item.get("enabled"))}

    @staticmethod
    def _discord(item: dict[str, Any], where: str) -> dict[str, Any]:
        guild = _text(item.get("guild"), 30)
        channels = _text(item.get("channels"), 300)
        if not GUILD_RE.fullmatch(guild):
            raise ValueError(f"{where}: the server id is a number (Developer Mode, right-click the server, Copy ID)")
        if not CHANNELS_RE.fullmatch(channels):
            raise ValueError(f"{where}: channels are names or ids separated by commas")
        return {
            "guild": guild,
            "label": _text(item.get("label"), 60),
            "channels": channels,
            "demo": _flag(item.get("demo"), False),
            "enabled": _flag(item.get("enabled")),
        }

    @staticmethod
    def _reddit(item: dict[str, Any], where: str) -> dict[str, Any]:
        name = _text(item.get("subreddit"), 30).removeprefix("/").removeprefix("r/")
        if not SUBREDDIT_RE.fullmatch(name):
            raise ValueError(f"{where}: {name or 'empty'!r} is not a subreddit name")
        return {"subreddit": name, "enabled": _flag(item.get("enabled"))}

    @staticmethod
    def _feeds(item: dict[str, Any], where: str) -> dict[str, Any]:
        url, label = _text(item.get("url"), 500), _text(item.get("label"), 60)
        if not url.startswith("https://") or any(c.isspace() for c in url):
            raise ValueError(f"{where}: an https feed URL is expected")
        return {
            "url": url,
            "label": label or url.removeprefix("https://").split("/")[0],
            "enabled": _flag(item.get("enabled")),
        }

    # --- What the collectors use ----------------------------------------------------------------

    def enabled_forums(self) -> list[dict[str, Any]]:
        return [f for f in self.forums if f["enabled"]]

    def repos(self) -> list[str]:
        return [g["repo"] for g in self.github if g["enabled"]]

    def guilds(self) -> list[dict[str, Any]]:
        return [d for d in self.discord if d["enabled"]]

    def subreddits(self) -> list[str]:
        return [r["subreddit"] for r in self.reddit if r["enabled"]]

    def imported_feeds(self) -> list[dict[str, str]]:
        """Feeds an external collector (the n8n workflow) reads and pushes to /ingest."""
        return [
            {"source": "reddit", "channel": f"r/{s}", "url": f"https://www.reddit.com/r/{s}/new/.rss?limit=100"}
            for s in self.subreddits()
        ] + [{"source": "rss", "channel": f["label"], "url": f["url"]} for f in self.feeds if f["enabled"]]

    def allows_import(self, source: str, channel: str) -> bool:
        """Imported reddit/rss threads are shown only while their channel is configured and enabled."""
        if source not in ("reddit", "rss"):
            return True
        return any(f["source"] == source and f["channel"] == channel for f in self.imported_feeds())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SourceStore:
    """The saved configuration (JSON file when `path` is set), with environment defaults until the first save."""

    def __init__(self, path: str | None = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self.config = SourceConfig.defaults()
        if self.path and self.path.exists():
            self.config = SourceConfig.parse(json.loads(self.path.read_text()))

    def save(self, data: Any) -> SourceConfig:
        config = SourceConfig.parse(data)
        with self._lock:
            if self.path:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(config.to_dict(), ensure_ascii=False, indent=1))
                tmp.replace(self.path)
            self.config = config
        return config
