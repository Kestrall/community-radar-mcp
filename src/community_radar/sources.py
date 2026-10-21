"""Collectors for the public community channels: OVHcloud forum (Discourse), GitHub issues and Reddit."""

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

from .models import Thread
from .products import classify

USER_AGENT = "community-radar-mcp/0.1 (+https://github.com/Kestrall/community-radar-mcp)"
FORUM = "https://community.ovhcloud.com"
FORUM_CATEGORY = "public-cloud/11"
ROADMAP_REPO = "ovh/public-cloud-roadmap"
ISSUE_REPOS = ("ovh/terraform-provider-ovh", "ovh/ovhcloud-cli")
SUBREDDIT = "ovh"


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

    async def _forum(self) -> list[Thread]:
        threads: list[Thread] = []
        for page in range(self.forum_pages):
            data = await self._json(f"{FORUM}/c/{FORUM_CATEGORY}/l/latest.json", page=page)
            topics = data["topic_list"]["topics"]
            for t in topics:
                if t.get("pinned") or "a-propos" in [tag.get("slug") for tag in t.get("tags", [])]:
                    continue
                threads.append(
                    classify(
                        Thread(
                            source="forum",
                            id=str(t["id"]),
                            title=html.unescape(t["title"]),
                            url=f"{FORUM}/t/{t['slug']}/{t['id']}",
                            created=parse_date(t["created_at"]),
                            last_activity=parse_date(t.get("last_posted_at") or t["created_at"]),
                            excerpt=html_to_text(t.get("excerpt") or "")[:300],
                            replies=t.get("reply_count", 0),
                            views=t.get("views", 0),
                            reactions=t.get("like_count", 0),
                            answered=t.get("posts_count", 1) > 1,
                            solved=bool(t.get("has_accepted_answer")),
                            labels=[tag["name"] for tag in t.get("tags", []) if isinstance(tag, dict)],
                            channel="community.ovhcloud.com",
                        )
                    )
                )
            if not data["topic_list"].get("more_topics_url") or not topics:
                break
        return threads

    async def forum_topic(self, topic_id: str) -> tuple[str, list[Post]]:
        """Title and full conversation of a forum topic."""

        async def fetch() -> tuple[str, list[Post]]:
            data = await self._json(f"{FORUM}/t/{topic_id}.json")
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

        return await self.cache.get(f"forum-topic:{topic_id}", fetch)

    async def first_response(self, thread: Thread) -> tuple[datetime | None, bool]:
        """Time of the first reply by someone else than the author, and whether staff ever replied."""
        if thread.source != "forum" or not thread.answered:
            return None, False
        try:
            _, posts = await self.forum_topic(thread.id)
        except httpx.HTTPError:
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
        """r/ovh newest posts through the official API (needs approved REDDIT_CLIENT_ID/SECRET)."""
        response = await self.client.get(
            f"https://oauth.reddit.com/r/{SUBREDDIT}/new",
            params={"limit": 100, "raw_json": 1},
            headers={"Authorization": f"Bearer {await self._reddit_token()}"},
        )
        response.raise_for_status()
        return [self._reddit_post(child["data"]) for child in response.json()["data"]["children"]]

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
                channel=f"r/{SUBREDDIT}",
            )
        )

    # --- Public API ---------------------------------------------------------------------------

    async def threads(self, refresh: bool = False) -> list[Thread]:
        """Recent community activity: forum, Terraform/CLI issues, roadmap updates and Reddit (when enabled)."""
        since = datetime.now(UTC) - timedelta(days=120)

        def guarded(name: str, fetch: Callable[[], Awaitable[list[Thread]]]) -> Awaitable[list[Thread]]:
            return self.cache.get(name, lambda: self._guarded(name, fetch), refresh, self._succeeded(name))

        fetches = [
            guarded("forum", self._forum),
            guarded("roadmap-recent", lambda: self._issues(ROADMAP_REPO, "roadmap", 2, since)),
            *(guarded(repo, lambda r=repo: self._issues(r, "github", 2, since)) for repo in ISSUE_REPOS),
        ]
        if self.reddit_enabled:
            fetches.append(
                self.reddit_cache.get(
                    "reddit", lambda: self._guarded("reddit", self._reddit), refresh, self._succeeded("reddit")
                )
            )
        batches = await asyncio.gather(*fetches)
        return [t for batch in batches for t in batch]

    async def roadmap(self, refresh: bool = False) -> list[Thread]:
        """The whole public roadmap (open and closed issues), cached longer: it changes slowly."""
        return await self.slow_cache.get(
            "roadmap-all",
            lambda: self._guarded("roadmap", lambda: self._issues(ROADMAP_REPO, "roadmap", 10)),  # API cap: 1000
            refresh,
            self._succeeded("roadmap"),
        )

    async def issue_conversation(self, repo: str, number: int) -> tuple[Thread, list[Post]]:
        """A GitHub issue with its full body and comments (maintainers flagged as staff)."""
        base = f"https://api.github.com/repos/{repo}/issues/{number}"
        issue, comments = await asyncio.gather(self._json(base), self._json(f"{base}/comments", per_page=50))
        source = "roadmap" if repo == ROADMAP_REPO else "github"
        thread = self._issue(issue, repo, source)
        opening = Post(thread.author, thread.created, issue.get("body") or "", False)
        return thread, [opening] + [
            Post(
                author=c["user"]["login"],
                created=parse_date(c["created_at"]),
                text=c.get("body") or "",
                staff=c.get("author_association") in ("OWNER", "MEMBER", "COLLABORATOR"),
            )
            for c in comments
        ]
