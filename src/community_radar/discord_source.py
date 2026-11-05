"""Discord channel: reads a server through a bot the server admins invited (official REST API, read-only).

The radar never scrapes Discord and never uses a user account: Discord only allows bots, and a bot only sees the
servers and channels it was invited to. Three kinds of conversations are read:

- forum channels (and media channels): each post is a thread with a title and tags, the closest to a forum topic;
- threads opened from a message in a text channel;
- questions asked in a text channel, with the replies that point to them (Discord "reply" links).

Staff are recognised by their roles (DISCORD_STAFF_ROLES, a regular expression on role names).
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from .models import Thread
from .products import classify

API = "https://discord.com/api/v10"
DISCORD_EPOCH_MS = 1420070400000
TEXT, ANNOUNCEMENT, FORUM, MEDIA = 0, 5, 15, 16
PUBLIC_THREAD = 11
# Plain messages and replies; joins, pins and other system messages are ignored.
MESSAGE_TYPES = (0, 19)
DEFAULT_STAFF_ROLES = r"staff|team|ovhcloud|admin|modo|moderat"
SOLVED_TAGS = re.compile(r"solved|r[ée]solu|resolved|fixed|answered|r[ée]pondu", re.IGNORECASE)
# A text-channel message is a question worth tracking when it is long enough or asks something.
MIN_QUESTION_CHARS = 40


def snowflake_time(snowflake: str | int) -> datetime:
    """Creation time encoded in every Discord id."""
    return datetime.fromtimestamp(((int(snowflake) >> 22) + DISCORD_EPOCH_MS) / 1000, UTC)


def time_snowflake(moment: datetime) -> str:
    """Smallest Discord id created at `moment`, for `before`/`after` pagination."""
    return str(int(moment.timestamp() * 1000 - DISCORD_EPOCH_MS) << 22)


def display_name(user: dict[str, Any] | None) -> str:
    user = user or {}
    return user.get("global_name") or user.get("username") or ""


def _is_question(message: dict[str, Any]) -> bool:
    content = (message.get("content") or "").strip()
    return len(content) >= MIN_QUESTION_CHARS or ("?" in content and len(content) >= 15)


def _title(content: str) -> str:
    first = next((line.strip() for line in content.splitlines() if line.strip()), "")
    return first if len(first) <= 120 else first[:117].rstrip() + "…"


class DiscordSource:
    """Reads one Discord server (guild) with a bot token. Off unless DISCORD_BOT_TOKEN and DISCORD_GUILD_ID are set."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        token: str | None = None,
        guild: str | None = None,
        channels: str | None = None,
        demo: bool | None = None,
        label: str = "",
    ) -> None:
        self.client = client
        self.token = token if token is not None else os.getenv("DISCORD_BOT_TOKEN", "")
        self.guild = guild if guild is not None else os.getenv("DISCORD_GUILD_ID", "")
        self.label = label
        wanted = channels if channels is not None else os.getenv("DISCORD_CHANNELS", "")
        self.channel_filter = {c.strip().lstrip("#").lower() for c in wanted.split(",") if c.strip()}
        self.staff_pattern = re.compile(os.getenv("DISCORD_STAFF_ROLES") or DEFAULT_STAFF_ROLES, re.IGNORECASE)
        self.pages = int(os.getenv("DISCORD_PAGES", "5"))
        # Webhook posts are usually feeds (releases, alerts); a demo server can use them to play several members.
        self.include_webhooks = os.getenv("DISCORD_INCLUDE_WEBHOOKS", "").lower() in ("1", "true", "yes")
        # A demo server holds fictional conversations: they are flagged so reports never pass them off as real.
        self.demo = demo if demo is not None else os.getenv("DISCORD_DEMO", "").lower() in ("1", "true", "yes")
        self.guild_name = ""
        self.enabled = bool(self.token and self.guild)
        self._members: dict[str, bool] = {}
        self._staff_roles: set[str] | None = None
        self._channels: dict[str, dict[str, Any]] = {}
        self._gate = asyncio.Semaphore(4)

    @property
    def name(self) -> str:
        """Label set on the Sources page, else the server name read by the bot, else its id."""
        return self.label or self.guild_name or self.guild

    # --- HTTP ---------------------------------------------------------------------------------

    async def _get(self, path: str, **params: Any) -> Any:
        """GET with the bot token; waits and retries when Discord asks to slow down (HTTP 429)."""
        headers = {"Authorization": f"Bot {self.token}"}
        async with self._gate:
            for _ in range(4):
                response = await self.client.get(f"{API}{path}", params=params or None, headers=headers)
                if response.status_code != 429:
                    break
                await asyncio.sleep(min(float(response.json().get("retry_after", 1)), 10))
        if response.status_code in (401, 403):
            raise ValueError(
                f"Discord refused {path} (HTTP {response.status_code}): check DISCORD_BOT_TOKEN, that the bot is in "
                "the server, can view the channel and read its history, and has the Message Content intent."
            )
        response.raise_for_status()
        return response.json()

    # --- Server layout and staff --------------------------------------------------------------

    async def channels(self) -> dict[str, dict[str, Any]]:
        """Readable channels of the server, by id (text, forum and media channels, optionally filtered)."""
        if not self._channels:
            self.guild_name = (await self._get(f"/guilds/{self.guild}")).get("name", "")
            found = await self._get(f"/guilds/{self.guild}/channels")
            self._channels = {
                c["id"]: c
                for c in found
                if c.get("type") in (TEXT, FORUM, MEDIA)
                and (
                    not self.channel_filter
                    or c["id"] in self.channel_filter
                    or c["name"].lower() in self.channel_filter
                )
            }
        return self._channels

    async def staff_roles(self) -> set[str]:
        if self._staff_roles is None:
            roles = await self._get(f"/guilds/{self.guild}/roles")
            self._staff_roles = {r["id"] for r in roles if self.staff_pattern.search(r.get("name", ""))}
        return self._staff_roles

    async def is_staff(self, user_id: str) -> bool:
        """Whether a member holds a staff role. One lookup per member, kept for the life of the process."""
        if user_id not in self._members:
            try:
                member = await self._get(f"/guilds/{self.guild}/members/{user_id}")
                self._members[user_id] = bool(set(member.get("roles", [])) & await self.staff_roles())
            except httpx.HTTPStatusError:  # left the server
                self._members[user_id] = False
        return self._members[user_id]

    # --- Collection ---------------------------------------------------------------------------

    def url(self, channel: str, message: str | None = None) -> str:
        return f"https://discord.com/channels/{self.guild}/{channel}" + (f"/{message}" if message else "")

    async def threads(self, since: datetime) -> list[Thread]:
        """Conversations active since `since` in every readable channel."""
        channels = await self.channels()
        active = await self._get(f"/guilds/{self.guild}/threads/active")
        discord_threads = {t["id"]: t for t in active.get("threads", []) if t.get("parent_id") in channels}
        for channel in channels.values():
            if channel["type"] in (FORUM, MEDIA):
                for t in await self._archived(channel["id"], since):
                    discord_threads.setdefault(t["id"], t)
        found = await asyncio.gather(
            *(self._thread(t, channels[t["parent_id"]]) for t in discord_threads.values() if self._recent(t, since)),
            *(self._channel_questions(c, since) for c in channels.values() if c["type"] == TEXT),
        )
        threads: list[Thread] = []
        seen: set[str] = set()
        for item in found:
            for t in item if isinstance(item, list) else [item]:
                if t and t.id not in seen:
                    seen.add(t.id)
                    threads.append(t)
        return threads

    @staticmethod
    def _recent(t: dict[str, Any], since: datetime) -> bool:
        return snowflake_time(t.get("last_message_id") or t["id"]) >= since

    async def _archived(self, channel_id: str, since: datetime) -> list[dict[str, Any]]:
        threads: list[dict[str, Any]] = []
        before: str | None = None
        for _ in range(self.pages):
            params: dict[str, Any] = {"limit": 50}
            if before:
                params["before"] = before
            page = await self._get(f"/channels/{channel_id}/threads/archived/public", **params)
            batch = page.get("threads", [])
            threads += batch
            if not page.get("has_more") or not batch:
                break
            before = batch[-1]["thread_metadata"]["archive_timestamp"]
            if datetime.fromisoformat(before) < since:
                break
        return threads

    async def _thread(self, t: dict[str, Any], parent: dict[str, Any]) -> Thread | None:
        """A Discord thread: a forum post, or a thread opened from a text-channel message."""
        if t.get("type", PUBLIC_THREAD) != PUBLIC_THREAD:
            return None
        try:  # the starter message carries the question; its id is the thread id
            starter = await self._get(f"/channels/{t['id']}/messages/{t['id']}")
        except (httpx.HTTPStatusError, ValueError):
            starter = {}
        tags = {tag["id"]: tag["name"] for tag in parent.get("available_tags", [])}
        labels = [tags[i] for i in t.get("applied_tags", []) if i in tags]
        content = starter.get("content") or ""
        replies = t.get("message_count", 0)
        created = (t.get("thread_metadata") or {}).get("create_timestamp")
        return classify(
            Thread(
                source="discord",
                id=f"{t['id']}/{t['id']}",
                title=t.get("name") or _title(content),
                url=self.url(t["id"]),
                created=datetime.fromisoformat(created) if created else snowflake_time(t["id"]),
                last_activity=snowflake_time(t.get("last_message_id") or t["id"]),
                excerpt=re.sub(r"\s+", " ", content)[:300],
                author=display_name(starter.get("author")),
                replies=replies,
                reactions=sum(r.get("count", 0) for r in starter.get("reactions", [])),
                answered=replies > 0,
                solved=any(SOLVED_TAGS.search(name) for name in labels),
                labels=labels,
                channel=f"discord #{parent.get('name', '')}",
                demo=self.demo,
            )
        )

    async def _messages(self, channel_id: str, since: datetime) -> list[dict[str, Any]]:
        """Messages of a text channel since `since`, oldest first."""
        messages: list[dict[str, Any]] = []
        before: str | None = None
        for _ in range(self.pages):
            params: dict[str, Any] = {"limit": 100}
            if before:
                params["before"] = before
            batch = await self._get(f"/channels/{channel_id}/messages", **params)
            messages += batch
            if len(batch) < 100 or snowflake_time(batch[-1]["id"]) < since:
                break
            before = batch[-1]["id"]
        return sorted((m for m in messages if snowflake_time(m["id"]) >= since), key=lambda m: int(m["id"]))

    def _automated(self, message: dict[str, Any]) -> bool:
        """Posted by a bot (or by a webhook, unless webhooks play members on a demo server)."""
        if message.get("webhook_id"):
            return not self.include_webhooks
        return bool((message.get("author") or {}).get("bot"))

    async def _channel_questions(self, channel: dict[str, Any], since: datetime) -> list[Thread]:
        """Questions asked in a text channel, with the reply chains that point to them."""
        messages = await self._messages(channel["id"], since)
        root_of: dict[str, str] = {}
        replies: dict[str, list[dict[str, Any]]] = {}
        roots = []
        for m in messages:
            if m.get("type") not in MESSAGE_TYPES or self._automated(m):
                continue
            parent = (m.get("message_reference") or {}).get("message_id")
            if parent and parent in root_of:
                root_of[m["id"]] = root_of[parent]
                replies[root_of[parent]].append(m)
            elif not parent and not m.get("thread") and _is_question(m):  # threaded messages come as threads
                root_of[m["id"]] = m["id"]
                replies[m["id"]] = []
                roots.append(m)
        threads = []
        for m in roots:
            author_id = m["author"]["id"]
            chain = replies[m["id"]]
            content = m.get("content") or ""
            threads.append(
                classify(
                    Thread(
                        source="discord",
                        id=f"{channel['id']}/{m['id']}",
                        title=_title(content),
                        url=self.url(channel["id"], m["id"]),
                        created=snowflake_time(m["id"]),
                        last_activity=snowflake_time(chain[-1]["id"] if chain else m["id"]),
                        excerpt=re.sub(r"\s+", " ", content)[:300],
                        author=display_name(m.get("author")),
                        replies=len(chain),
                        reactions=sum(r.get("count", 0) for r in m.get("reactions", [])),
                        answered=any(r["author"]["id"] != author_id for r in chain),
                        channel=f"discord #{channel.get('name', '')}",
                        demo=self.demo,
                    )
                )
            )
        return threads

    # --- Conversations ------------------------------------------------------------------------

    async def conversation(self, channel_id: str, root_id: str) -> list[dict[str, Any]]:
        """Every message of a conversation, oldest first, each with a `staff` flag.

        A thread (channel_id == root_id, or a text-channel message that opened a thread) is read whole; a plain
        text-channel question is read with the reply chain that points to it.
        """
        if channel_id == root_id:
            messages = await self._get(f"/channels/{channel_id}/messages", limit=100, after=str(int(root_id) - 1))
        else:
            root = await self._get(f"/channels/{channel_id}/messages/{root_id}")
            if root.get("thread"):
                return await self.conversation(root_id, root_id)
            later = await self._get(f"/channels/{channel_id}/messages", limit=100, after=root_id)
            chain, messages = {root_id}, [root]
            for m in sorted(later, key=lambda m: int(m["id"])):
                if (m.get("message_reference") or {}).get("message_id") in chain:
                    chain.add(m["id"])
                    messages.append(m)
        messages = sorted(
            (m for m in messages if m.get("type") in MESSAGE_TYPES or m["id"] == root_id), key=lambda m: int(m["id"])
        )
        flags = await asyncio.gather(*(self.is_staff(m["author"]["id"]) for m in messages))
        return [{**m, "staff": staff} for m, staff in zip(messages, flags, strict=True)]
