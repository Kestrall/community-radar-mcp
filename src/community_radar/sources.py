"""Collectors for the community channels: Discourse forums, GitHub issues, Reddit, Discord and imported feeds."""

from __future__ import annotations

import asyncio
import html
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .config import SourceConfig, SourceStore
from .discord_source import DEFAULT_STAFF_ROLES, DiscordSource, display_name, snowflake_time
from .docs import DocsIndex
from .inbox import Inbox
from .models import Thread
from .products import classify, in_scope, use_taxonomy
from .profiles import Profile, resolve
from .triage import TriageStore

USER_AGENT = "community-radar-mcp/0.1 (+https://github.com/Kestrall/community-radar-mcp)"
NO_DOCS = DocsIndex([])
# A Discourse topic URL: https://forum.example.com[/sub/path]/t/<slug>/<id>
TOPIC_RE = re.compile(r"^(?P<base>https://[^\s]+?)/t/(?:[^/?#\s]+/)?(?P<id>\d+)")
STAFF_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")


def parse_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def html_to_text(value: str) -> str:
    text = re.sub(r"<(pre|code)[^>]*>", "\n```\n", value)
    text = re.sub(r"</(pre|code)>", "\n```\n", text)
    text = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text)).replace("\xa0", " ")
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


class RateLimited(ValueError):
    """An API quota is exhausted; the message tells the user how to fix it."""


@dataclass
class Post:
    author: str
    created: datetime
    text: str
    staff: bool = False


class TTLCache:
    """Tiny async cache: one in-flight fetch per key, results kept for `ttl` seconds."""

    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._data: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(
        self,
        key: str,
        fetch: Callable[[], Awaitable[Any]],
        refresh: bool = False,
        keep: Callable[[Any], bool] = lambda value: True,
    ) -> Any:
        """Cached value of `fetch()`; results rejected by `keep` (e.g. a failed fetch) are retried next time."""
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._data.get(key)
            if hit and not refresh and time.monotonic() - hit[0] < self.ttl:
                return hit[1]
            value = await fetch()
            if keep(value):
                self._data[key] = (time.monotonic(), value)
            else:
                self._data.pop(key, None)
            return value


class Collector:
    """Fetches and caches community threads from every channel. Failing channels are reported, not fatal."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        ttl: float = 900,
        github_token: str | None = None,
        forum_pages: int = 6,
    ) -> None:
        headers = {"User-Agent": USER_AGENT}
        self.client = client or httpx.AsyncClient(timeout=20, follow_redirects=True, headers=headers)
        self.cache = TTLCache(ttl)
        self.slow_cache = TTLCache(6 * 3600)  # the full roadmap changes slowly and costs ~10 GitHub calls
        self.reddit_cache = TTLCache(3600)
        self.github_token = github_token if github_token is not None else os.getenv("GITHUB_TOKEN")
        self.forum_pages = forum_pages
        self.reddit_id = os.getenv("REDDIT_CLIENT_ID")
        self.reddit_secret = os.getenv("REDDIT_CLIENT_SECRET")
        # Reddit requires approved API access (Responsible Builder Policy): the channel is off without credentials.
        self.reddit_enabled = bool(self.reddit_id and self.reddit_secret)
        self.errors: dict[str, str] = {}
        # Watched repositories, Discord servers, subreddits and feeds (RADAR_SOURCES_PATH, edited on /sources).
        self.sources = SourceStore(os.getenv("RADAR_SOURCES_PATH"))
        # Discord only through a bot invited by each server's admins: one reader per configured server.
        self.discords: dict[str, DiscordSource] = {}
        self.apply_sources(self.sources.config)
        # Threads pushed by an external collector through POST /ingest (see inbox.py).
        self.inbox = Inbox.from_env()
        # Triage statuses and history (RADAR_TRIAGE_PATH: SQLite file; in memory when unset).
        self.triage = TriageStore(os.getenv("RADAR_TRIAGE_PATH"))

    # --- HTTP helpers -------------------------------------------------------------------------

    async def _json(self, url: str, **params: Any) -> Any:
        headers = {"Accept": "application/json"}
        if url.startswith("https://api.github.com") and self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        response = await self.client.get(url, params=params or None, headers=headers)
        if response.status_code in (403, 429) and response.headers.get("x-ratelimit-remaining") == "0":
            raise RateLimited("GitHub API rate limit reached: set GITHUB_TOKEN (a read-only token is enough).")
        response.raise_for_status()
        return response.json()

    def _succeeded(self, name: str) -> Callable[[Any], bool]:
        return lambda _: name not in self.errors

    async def _guarded(self, name: str, fetch: Callable[[], Awaitable[list[Thread]]]) -> list[Thread]:
        try:
            threads = await fetch()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            self.errors[name] = f"{type(exc).__name__}: {exc}"[:200]
            return []
        self.errors.pop(name, None)
        return threads

    # --- Forum (Discourse) --------------------------------------------------------------------

    async def _forum(self, forum: dict[str, Any]) -> list[Thread]:
        """Latest topics of a Discourse forum, or of one of its categories."""
        base, category = forum["url"], forum["category"]
        listing = f"{base}/c/{category}/l/latest.json" if category else f"{base}/latest.json"
        threads: list[Thread] = []
        for page in range(self.forum_pages):
            data = await self._json(listing, page=page)
            topics = data["topic_list"]["topics"]
            for t in topics:
                tags = [tag.get("slug") if isinstance(tag, dict) else tag for tag in t.get("tags", [])]
                if t.get("pinned") or {"a-propos", "about"} & set(tags):  # category "About" topics
                    continue
                threads.append(
                    classify(
                        Thread(
                            source="forum",
                            id=str(t["id"]),
                            title=html.unescape(t["title"]),
                            url=f"{base}/t/{t['slug']}/{t['id']}",
                            created=parse_date(t["created_at"]),
                            last_activity=parse_date(t.get("last_posted_at") or t["created_at"]),
                            excerpt=html_to_text(t.get("excerpt") or "")[:300],
                            replies=t.get("reply_count", 0),
                            views=t.get("views", 0),
                            reactions=t.get("like_count", 0),
                            answered=t.get("posts_count", 1) > 1,
                            solved=bool(t.get("has_accepted_answer")),
                            labels=[tag["name"] for tag in t.get("tags", []) if isinstance(tag, dict)],
                            channel=forum["label"],
                        )
                    )
                )
            if not data["topic_list"].get("more_topics_url") or not topics:
                break
        return threads

    def forum_topic_ref(self, url: str) -> tuple[str, str] | None:
        """(forum base URL, topic id) of a Discourse topic link, preferring the configured forums' addresses."""
        match = TOPIC_RE.match(url)
        if not match:
            return None
        base = next((f["url"] for f in self.config.forums if url.startswith(f["url"] + "/t/")), match["base"])
        return base, match["id"]

    async def forum_topic(self, url: str) -> tuple[str, list[Post]]:
        """Title and full conversation of a forum topic, from its URL."""
        ref = self.forum_topic_ref(url)
        if not ref:
            raise ValueError(f"Not a forum topic URL: {url}")
        base, topic_id = ref

        async def fetch() -> tuple[str, list[Post]]:
            data = await self._json(f"{base}/t/{topic_id}.json")
            posts = [
                Post(
                    author=p["username"],
                    created=parse_date(p["created_at"]),
                    text=html_to_text(p.get("cooked", "")),
                    staff=bool(p.get("staff") or p.get("admin") or p.get("moderator")),
                )
                for p in data["post_stream"]["posts"]
            ]
            return html.unescape(data.get("title", "")), posts

        return await self.cache.get(f"forum-topic:{base}/{topic_id}", fetch)

    async def first_response(self, thread: Thread) -> tuple[datetime | None, bool]:
        """Time of the first reply by someone else than the author, and whether staff ever replied."""
        if thread.source != "forum" or not thread.answered:
            return None, False
        try:
            _, posts = await self.forum_topic(thread.url)
        except (httpx.HTTPError, ValueError):
            return None, False
        author = posts[0].author if posts else ""
        others = [p for p in posts[1:] if p.author != author]
        return (others[0].created if others else None), any(p.staff for p in posts[1:])

    # --- GitHub -------------------------------------------------------------------------------

    def _issue(self, issue: dict[str, Any], repo: str, source: str) -> Thread:
        reactions = issue.get("reactions") or {}
        return classify(
            Thread(
                source=source,
                id=f"{repo}#{issue['number']}",
                title=issue["title"],
                url=issue["html_url"],
                created=parse_date(issue["created_at"]),
                last_activity=parse_date(issue["updated_at"]),
                excerpt=re.sub(r"\s+", " ", issue.get("body") or "")[:300],
                author=(issue.get("user") or {}).get("login", ""),
                replies=issue.get("comments", 0),
                reactions=reactions.get("+1", 0) + reactions.get("heart", 0) + reactions.get("hooray", 0),
                answered=issue.get("comments", 0) > 0,
                solved=issue.get("state") == "closed",
                labels=[lbl["name"] for lbl in issue.get("labels", [])],
                channel=repo,
                staff_author=issue.get("author_association") in STAFF_ASSOCIATIONS,
            )
        )

    async def _issues(self, repo: str, source: str, pages: int, since: datetime | None = None) -> list[Thread]:
        threads: list[Thread] = []
        for page in range(1, pages + 1):
            params: dict[str, Any] = {"state": "all", "sort": "updated", "per_page": 100, "page": page}
            if since:
                params["since"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
            batch = await self._json(f"https://api.github.com/repos/{repo}/issues", **params)
            threads += [self._issue(i, repo, source) for i in batch if "pull_request" not in i]
            if len(batch) < 100:
                break
        return threads

    # --- Reddit -------------------------------------------------------------------------------

    async def _reddit_token(self) -> str:
        """Application-only OAuth token (client credentials): enough to read public subreddits."""

        async def fetch() -> str:
            response = await self.client.post(
                "https://www.reddit.com/api/v1/access_token",
                data={"grant_type": "client_credentials"},
                auth=(self.reddit_id, self.reddit_secret),
            )
            response.raise_for_status()
            return response.json()["access_token"]

        return await self.slow_cache.get("reddit-token", fetch)

    async def _reddit(self) -> list[Thread]:
        """Newest posts of the configured subreddits through the official API (needs approved credentials)."""
        threads: list[Thread] = []
        for subreddit in self.config.subreddits():
            response = await self.client.get(
                f"https://oauth.reddit.com/r/{subreddit}/new",
                params={"limit": 100, "raw_json": 1},
                headers={"Authorization": f"Bearer {await self._reddit_token()}"},
            )
            response.raise_for_status()
            threads += [self._reddit_post(child["data"]) for child in response.json()["data"]["children"]]
        return threads

    @staticmethod
    def _reddit_post(post: dict[str, Any]) -> Thread:
        created = datetime.fromtimestamp(post["created_utc"], UTC)
        edited = post.get("edited")
        return classify(
            Thread(
                source="reddit",
                id=post["name"],
                title=post["title"],
                url=f"https://www.reddit.com{post['permalink']}",
                created=created,
                last_activity=datetime.fromtimestamp(edited, UTC) if isinstance(edited, (int, float)) else created,
                excerpt=re.sub(r"\s+", " ", post.get("selftext") or "")[:300],
                author=post.get("author", ""),
                replies=post.get("num_comments", 0),
                reactions=max(post.get("score", 0), 0),
                answered=post.get("num_comments", 0) > 0,
                labels=[post["link_flair_text"]] if post.get("link_flair_text") else [],
                channel=f"r/{post.get('subreddit', '')}",
            )
        )

    # --- Discord ------------------------------------------------------------------------------

    @property
    def discord_enabled(self) -> bool:
        return any(d.enabled for d in self.discords.values())

    def discord_for(self, url: str) -> DiscordSource:
        """The reader of the server a discord.com/channels/<server>/... link belongs to."""
        match = re.search(r"/channels/(\d+)/", url)
        source = self.discords.get(match.group(1)) if match else None
        if not source or not source.enabled:
            raise ValueError(
                "This Discord server is not configured on the Sources page (or DISCORD_BOT_TOKEN is unset)."
            )
        return source

    async def discord_topic(self, thread_id: str, url: str) -> list[Post]:
        """Full conversation of a Discord thread or question (`channel/message` id), staff flagged by role."""
        channel, root = thread_id.split("/")
        source = self.discord_for(url)

        async def fetch() -> list[Post]:
            return [
                Post(display_name(m.get("author")), snowflake_time(m["id"]), m.get("content") or "", m["staff"])
                for m in await source.conversation(channel, root)
            ]

        return await self.cache.get(f"discord-topic:{source.guild}:{thread_id}", fetch)

    # --- Configuration ------------------------------------------------------------------------

    @property
    def config(self) -> SourceConfig:
        return self.sources.config

    def apply_sources(self, config: SourceConfig) -> None:
        """(Re)build the readers from the configuration and drop cached data, so changes show at the next view."""
        self.profile: Profile = resolve(config)
        use_taxonomy(self.profile.taxonomy)
        staff_roles = "|".join(r for r in (DEFAULT_STAFF_ROLES, self.profile.staff_roles) if r)
        previous = self.discords
        self.discords = {}
        for d in config.guilds():
            reader = DiscordSource(
                self.client,
                guild=d["guild"],
                channels=d["channels"],
                demo=d["demo"],
                label=d["label"],
                staff_roles=staff_roles,
            )
            old = previous.get(d["guild"])
            if old and old.channel_filter == reader.channel_filter:
                reader.guild_name, reader._members, reader._staff_roles = old.guild_name, old._members, old._staff_roles
            self.discords[d["guild"]] = reader
        self.cache = TTLCache(self.cache.ttl)
        self.slow_cache = TTLCache(self.slow_cache.ttl)
        self.reddit_cache = TTLCache(self.reddit_cache.ttl)
        self.errors.clear()

    def save_sources(self, data: Any) -> SourceConfig:
        config = self.sources.save(data)
        self.apply_sources(config)
        return config

    def guides(self, docs: DocsIndex) -> DocsIndex:
        """The documentation index the active profile ships with (or a custom one set by RADAR_DOCS_INDEX)."""
        return docs if self.profile.docs or os.getenv("RADAR_DOCS_INDEX") else NO_DOCS

    def is_roadmap(self, repo: str) -> bool:
        return repo.lower() == self.config.roadmap.lower()

    # --- Public API ---------------------------------------------------------------------------

    async def threads(self, refresh: bool = False) -> list[Thread]:
        """Recent community activity: forum, Terraform/CLI issues, roadmap updates, Reddit and Discord (when
        enabled)."""
        since = datetime.now(UTC) - timedelta(days=120)

        def guarded(name: str, fetch: Callable[[], Awaitable[list[Thread]]]) -> Awaitable[list[Thread]]:
            return self.cache.get(name, lambda: self._guarded(name, fetch), refresh, self._succeeded(name))

        roadmap = self.config.roadmap
        fetches = [
            *(guarded(f"forum:{f['label']}", lambda f=f: self._forum(f)) for f in self.config.enabled_forums()),
            *([guarded("roadmap-recent", lambda: self._issues(roadmap, "roadmap", 2, since))] if roadmap else []),
            *(guarded(repo, lambda r=repo: self._issues(r, "github", 2, since)) for repo in self.config.repos()),
        ]
        if self.reddit_enabled:
            fetches.append(
                self.reddit_cache.get(
                    "reddit", lambda: self._guarded("reddit", self._reddit), refresh, self._succeeded("reddit")
                )
            )
        for guild, reader in self.discords.items():
            if reader.enabled:
                fetches.append(guarded(f"discord:{guild}", lambda r=reader: r.threads(since)))
        batches = await asyncio.gather(*fetches)
        live = {t.source for batch in batches for t in batch} | ({"reddit"} if self.reddit_enabled else set())
        # Imported channels can cover more than the community (r/ovh: every OVHcloud product): the profile's scope.
        imported = [
            t
            for t in self.inbox.threads()
            if t.source not in live and self.config.allows_import(t.source, t.channel) and in_scope(t)
        ]
        return [t for batch in batches for t in batch] + imported

    async def roadmap(self, refresh: bool = False) -> list[Thread]:
        """The whole public roadmap (open and closed issues), cached longer: it changes slowly."""
        if not self.config.roadmap:
            return []
        return await self.slow_cache.get(
            "roadmap-all",
            lambda: self._guarded("roadmap", lambda: self._issues(self.config.roadmap, "roadmap", 10)),  # cap: 1000
            refresh,
            self._succeeded("roadmap"),
        )

    async def issue_conversation(self, repo: str, number: int) -> tuple[Thread, list[Post]]:
        """A GitHub issue with its full body and comments (maintainers flagged as staff)."""
        base = f"https://api.github.com/repos/{repo}/issues/{number}"
        issue, comments = await asyncio.gather(self._json(base), self._json(f"{base}/comments", per_page=50))
        source = "roadmap" if self.is_roadmap(repo) else "github"
        thread = self._issue(issue, repo, source)
        opening = Post(thread.author, thread.created, issue.get("body") or "", False)
        return thread, [opening] + [
            Post(
                author=c["user"]["login"],
                created=parse_date(c["created_at"]),
                text=c.get("body") or "",
                staff=c.get("author_association") in STAFF_ASSOCIATIONS,
            )
            for c in comments
        ]

    async def issue_timeline(self, repo: str, number: int) -> list[dict[str, Any]]:
        """Timeline events of a GitHub issue: comments, labels, assignments, milestones, linked pull requests."""
        return await self.cache.get(
            f"timeline:{repo}#{number}",
            lambda: self._json(f"https://api.github.com/repos/{repo}/issues/{number}/timeline", per_page=100),
        )
