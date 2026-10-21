"""MCP server: OVHcloud Public Cloud community radar."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from . import analytics
from .docs import DocsIndex
from .models import Guide, Thread
from .products import PRODUCTS, SIGNALS, label
from .products import resolve_product as _resolve_product
from .sources import FORUM, ISSUE_REPOS, ROADMAP_REPO, SUBREDDIT, Collector, RateLimited

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
INSTRUCTIONS = (
    "Community radar for OVHcloud Public Cloud: the official forum (community.ovhcloud.com), GitHub issues "
    "(public roadmap, Terraform provider, ovhcloud-cli), Reddit r/ovh when enabled, plus the official guides. "
    "Use community_pulse for metrics, find_irritants for pain points to report to product teams, "
    "unanswered_questions to triage, thread_context before drafting a reply, roadmap_status to check whether "
    "a request is already tracked, search_docs to point users to the right guide and content_gaps to find "
    "tutorial ideas. Always cite thread and guide URLs. Never claim something was posted: you only draft."
)


class ThreadOut(BaseModel):
    source: str
    channel: str
    title: str
    url: str
    created: datetime
    last_activity: datetime
    age_days: float
    replies: int
    views: int
    votes: int
    answered: bool | None
    solved: bool
    labels: list[str]
    products: list[str]
    signals: list[str]
    excerpt: str

    @classmethod
    def of(cls, t: Thread) -> ThreadOut:
        return cls(
            source=t.source,
            channel=t.channel,
            title=t.title,
            url=t.url,
            created=t.created,
            last_activity=t.last_activity,
            age_days=round((datetime.now(UTC) - t.created).total_seconds() / 86400, 1),
            replies=t.replies,
            views=t.views,
            votes=t.reactions,
            answered=t.answered,
            solved=t.solved,
            labels=t.labels,
            products=[label(p) for p in t.products],
            signals=t.signals,
            excerpt=t.excerpt[:200],
        )


class GuideOut(BaseModel):
    title: str
    excerpt: str
    url: str
    source: str
    section: str
    updated: str
    score: float | None = None

    @classmethod
    def of(cls, g: Guide, score: float | None = None, lang: str = "en") -> GuideOut:
        return cls(
            title=g.title,
            excerpt=g.excerpt,
            url=g.url(lang),
            source=f"https://github.com/ovh/docs/tree/develop/{g.path}",
            section=g.section,
            updated=g.updated,
            score=score,
        )


class PulseOut(BaseModel):
    period_days: int
    product: str | None
    active_threads: int
    new_threads: int
    previous_period_new_threads: int
    by_source: dict[str, int]
    by_product: dict[str, int]
    by_signal: dict[str, int]
    unanswered: int
    answered_rate: float | None
    solved_rate: float | None
    forum_median_first_response_hours: float | None
    forum_first_response_sample: int
    forum_staff_reply_rate: float | None
    most_engaging: list[ThreadOut]
    unavailable_sources: dict[str, str]


class IrritantOut(BaseModel):
    product: str
    active_threads: int
    new_threads: int
    previous_period_new_threads: int
    negative_threads: int
    signals: dict[str, int]
    engagement: float
    score: float
    examples: list[ThreadOut]


class GapOut(BaseModel):
    question: ThreadOut
    closest_guide: GuideOut | None
    match_score: float


class RoadmapOut(BaseModel):
    query: str
    matches: int
    open: int
    shipped_or_closed: int
    items: list[ThreadOut]


class PostOut(BaseModel):
    author: str
    date: datetime
    staff: bool
    text: str


class ContextOut(BaseModel):
    thread: ThreadOut
    posts: list[PostOut]
    related_guides: list[GuideOut]
    related_roadmap: list[ThreadOut]


def resolve_product(name: str | None) -> str | None:
    try:
        return _resolve_product(name)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def _cap(limit: int, maximum: int = 50) -> int:
    return max(1, min(limit, maximum))


def create_server(collector: Collector, docs: DocsIndex) -> MCPServer:
    mcp = MCPServer("community-radar", instructions=INSTRUCTIONS, version="0.1.0")

    @mcp.tool(annotations=READ_ONLY)
    async def community_pulse(days: int = 7, product: str | None = None, refresh: bool = False) -> PulseOut:
        """Activity metrics over the last `days`: volume by channel and product, signals (incident, bug, billing,
        security, feature request), unanswered threads, solved rate and forum first-response time.

        product: optional filter, e.g. "kubernetes", "object storage", "AI".
        """
        key = resolve_product(product)
        threads = await collector.threads(refresh)
        p = await analytics.pulse(collector, threads, days, key)
        return PulseOut(
            period_days=days,
            product=label(key) if key else None,
            active_threads=p.total,
            new_threads=p.new_threads,
            previous_period_new_threads=p.previous_new_threads,
            by_source=p.by_source,
            by_product=p.by_product,
            by_signal=p.by_signal,
            unanswered=p.unanswered,
            answered_rate=p.answered_rate,
            solved_rate=p.solved_rate,
            forum_median_first_response_hours=p.median_first_response_hours,
            forum_first_response_sample=p.first_response_sample,
            forum_staff_reply_rate=p.staff_reply_rate,
            most_engaging=[ThreadOut.of(t) for t in p.top_threads],
            unavailable_sources=dict(collector.errors),
        )

    @mcp.tool(annotations=READ_ONLY)
    async def find_irritants(days: int = 14, limit: int = 5) -> list[IrritantOut]:
        """Products ranked by pain: negative signals (incidents, bugs, billing, security), engagement and growth
        of new threads versus the previous period, with example threads. Input for a report to product managers."""
        threads = await collector.threads()
        return [
            IrritantOut(
                product=i.label,
                active_threads=i.threads,
                new_threads=i.new_threads,
                previous_period_new_threads=i.previous_threads,
                negative_threads=i.negative_threads,
                signals=i.signals,
                engagement=i.engagement,
                score=i.score,
                examples=[ThreadOut.of(t) for t in i.examples],
            )
            for i in analytics.irritants(threads, days, _cap(limit, 12))
        ]

    @mcp.tool(annotations=READ_ONLY)
    async def unanswered_questions(days: int = 30, product: str | None = None, limit: int = 10) -> list[ThreadOut]:
        """Forum topics and GitHub issues active in the last `days` with no reply yet, most urgent first
        (age weighted by views)."""
        threads = await collector.threads()
        return [ThreadOut.of(t) for t in analytics.unanswered(threads, days, resolve_product(product))[: _cap(limit)]]

    @mcp.tool(annotations=READ_ONLY)
    async def search_threads(
        query: str, days: int = 180, source: str | None = None, limit: int = 10
    ) -> list[ThreadOut]:
        """Keyword search across community threads (titles, excerpts, labels).

        source: forum, github, roadmap or reddit.
        """
        threads = analytics.window(await collector.threads(), days)
        if source:
            threads = [t for t in threads if t.source == source.lower()]
        return [ThreadOut.of(t) for t in analytics.search_threads(threads, query)[: _cap(limit)]]

    @mcp.tool(annotations=READ_ONLY)
    async def roadmap_status(query: str, limit: int = 8) -> RoadmapOut:
        """Is a feature already on the public roadmap (github.com/ovh/public-cloud-roadmap)? Returns matching items
        with their state, labels and community votes (👍)."""
        roadmap = await collector.roadmap()
        if not roadmap and "roadmap" in collector.errors:
            raise ToolError(f"Roadmap unavailable: {collector.errors['roadmap']}")
        found = analytics.search_threads(roadmap, query, by_votes=True)
        return RoadmapOut(
            query=query,
            matches=len(found),
            open=sum(1 for t in found if not t.solved),
            shipped_or_closed=sum(1 for t in found if t.solved),
            items=[ThreadOut.of(t) for t in found[: _cap(limit)]],
        )

    @mcp.tool(annotations=READ_ONLY)
    def search_docs(query: str, lang: str = "en", section: str | None = None, limit: int = 5) -> list[GuideOut]:
        """Find official OVHcloud Public Cloud guides for a question. lang: en or fr (link language).

        section: optional guide folder, e.g. containers_orchestration, storage_and_backup, compute, network,
        ai_machine_learning, public_cloud_databases.
        """
        return [GuideOut.of(g, s, lang) for g, s in docs.search(query, _cap(limit, 20), section)]

    @mcp.tool(annotations=READ_ONLY)
    async def content_gaps(days: int = 60, limit: int = 8) -> list[GapOut]:
        """Community questions that no official guide clearly covers (weak documentation match): candidates for
        new tutorials, FAQ entries or doc fixes."""
        threads = await collector.threads()
        return [
            GapOut(
                question=ThreadOut.of(g.thread),
                closest_guide=GuideOut.of(g.best_guide, g.best_score) if g.best_guide else None,
                match_score=g.best_score,
            )
            for g in analytics.content_gaps(threads, docs, days, _cap(limit, 20))
        ]

    @mcp.tool(annotations=READ_ONLY)
    async def thread_context(url: str) -> ContextOut:
        """Everything needed to draft a reply to a forum topic or GitHub issue: the full conversation (who is
        staff), the closest official guides and related roadmap items. Drafting only: nothing is posted."""
        if forum := re.search(r"community\.ovhcloud\.com/t/(?:[^/]+/)?(\d+)", url):
            title, posts = await collector.forum_topic(forum.group(1))
            known = {t.id: t for t in await collector.threads() if t.source == "forum"}
            thread = known.get(forum.group(1)) or Thread(
                "forum", forum.group(1), title, url, posts[0].created, posts[-1].created, posts[0].text[:300]
            )
        elif issue := re.search(r"github\.com/([^/]+/[^/]+)/issues/(\d+)", url):
            try:
                thread, posts = await collector.issue_conversation(issue.group(1), int(issue.group(2)))
            except (RateLimited, httpx.HTTPStatusError) as exc:
                raise ToolError(f"Could not read this issue: {exc}") from exc
        else:
            raise ToolError("Supported URLs: community.ovhcloud.com/t/... and github.com/<owner>/<repo>/issues/<n>")
        related_docs = docs.search(f"{thread.title} {thread.excerpt[:120]}", 3)
        related_roadmap = analytics.search_threads(await collector.roadmap(), thread.title)[:3]
        return ContextOut(
            thread=ThreadOut.of(thread),
            posts=[PostOut(author=p.author, date=p.created, staff=p.staff, text=p.text[:1500]) for p in posts[:20]],
            related_guides=[GuideOut.of(g, s) for g, s in related_docs],
            related_roadmap=[ThreadOut.of(t) for t in related_roadmap if t.url != thread.url],
        )

    @mcp.resource("radar://sources", mime_type="text/markdown")
    def sources_resource() -> str:
        """Channels watched by the radar."""
        repos = "\n".join(f"- GitHub issues: https://github.com/{r}" for r in ISSUE_REPOS)
        return (
            f"- Forum, Public Cloud category: {FORUM}/c/public-cloud/11\n"
            f"- Public roadmap: https://github.com/{ROADMAP_REPO}\n{repos}\n"
            + (f"- Reddit: https://www.reddit.com/r/{SUBREDDIT}\n" if collector.reddit_enabled else "")
            + f"- Official guides: https://github.com/ovh/docs ({len(docs.guides)} Public Cloud guides indexed)\n"
        )

    @mcp.resource("radar://taxonomy", mime_type="text/markdown")
    def taxonomy_resource() -> str:
        """Products and signals used to classify threads."""
        products = "\n".join(f"- `{k}`: {v[0]}" for k, v in PRODUCTS.items())
        return f"## Products\n{products}\n\n## Signals\n" + "\n".join(f"- `{s}`" for s in SIGNALS)

    @mcp.prompt()
    def weekly_report(days: int = 7) -> str:
        """Weekly community report for Public Cloud product managers."""
        return (
            f"Build the weekly OVHcloud Public Cloud community report for the last {days} days. "
            f"Call community_pulse(days={days}), find_irritants(days={days}), unanswered_questions(days={days}) "
            f"and content_gaps(days={max(days, 30)}). Then write, in English, a concise report for product "
            "managers: 1) headline numbers with the trend versus the previous period, 2) top 3 irritants with "
            "user quotes or thread titles and links, and what you recommend to each product team, "
            "3) questions waiting for an answer, 4) two tutorial or FAQ ideas. Cite every thread URL."
        )

    @mcp.prompt()
    def draft_reply(url: str, language: str = "same as the user") -> str:
        """Draft a helpful, on-brand reply to a community thread."""
        return (
            f"Call thread_context(url={url!r}). Draft a reply in {language} as an OVHcloud Tech Community "
            "Manager: thank the user, restate the problem in one sentence, give concrete steps (commands, console "
            "paths, Terraform or CLI snippets) grounded in the related guides, link the guides, mention a "
            "related roadmap item if any, and ask for the missing details needed to reproduce. Stay factual: if "
            "the guides do not cover it, say you are checking with the product team. Then list, separately, "
            "what should be escalated internally (Support, Product or Engineering) and why. Do not post anything."
        )

    @mcp.prompt()
    def tutorial_from_gap(topic: str) -> str:
        """Outline a tutorial for a recurring community question that the docs do not cover."""
        return (
            f"Use search_threads({topic!r}), content_gaps() and search_docs({topic!r}) to understand what users "
            f"struggle with about {topic} and what the official guides already say. Then propose a tutorial: "
            "title, target reader, prerequisites, step-by-step outline with the commands or Terraform snippets to "
            "show, pitfalls seen in the threads, and the existing guides to link. Cite the threads that motivated "
            "it."
        )

    return mcp
