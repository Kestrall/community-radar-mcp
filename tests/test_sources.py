import httpx
import pytest
import respx

from community_radar.sources import FORUM, Collector, RateLimited, html_to_text

TOPIC = {
    "id": 54094,
    "title": "Object Storage S3 : 503",
    "slug": "s3-503",
    "created_at": "2026-10-07T10:00:00.000Z",
    "last_posted_at": "2026-10-08T10:00:00.000Z",
    "excerpt": "GET &amp; PUT fail",
    "reply_count": 2,
    "views": 77,
    "like_count": 1,
    "posts_count": 3,
    "has_accepted_answer": False,
    "tags": [{"name": "incident", "slug": "incident"}],
}
PINNED = {**TOPIC, "id": 29, "pinned": True, "title": "About"}
ISSUE = {
    "number": 300,
    "title": "Release assets replaced",
    "html_url": "https://github.com/ovh/ovhcloud-cli/issues/300",
    "created_at": "2026-10-20T08:00:00Z",
    "updated_at": "2026-10-21T08:00:00Z",
    "body": "Checksums changed",
    "user": {"login": "alice"},
    "comments": 2,
    "state": "open",
    "labels": [{"name": "bug"}],
    "reactions": {"+1": 3, "heart": 1, "hooray": 0},
}
REDDIT = {
    "data": {
        "children": [
            {
                "data": {
                    "name": "t3_b",
                    "title": "MKS upgrade failed",
                    "permalink": "/r/ovh/comments/b/",
                    "created_utc": 1792500000,
                    "edited": False,
                    "selftext": "node pool stuck",
                    "author": "carol",
                    "num_comments": 4,
                    "score": 7,
                    "link_flair_text": "Public Cloud",
                }
            }
        ]
    }
}


@pytest.fixture
def reddit_credentials(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")


def mock_all(router):
    router.get(url__regex=rf"{FORUM}/c/public-cloud/11/l/latest.json.*").respond(
        json={"topic_list": {"topics": [PINNED, TOPIC], "more_topics_url": None}}
    )
    router.get(f"{FORUM}/t/54094.json").respond(
        json={
            "title": "S3 503",
            "post_stream": {
                "posts": [
                    {"username": "alice", "created_at": "2026-10-07T10:00:00Z", "cooked": "<p>503 on GET</p>"},
                    {"username": "alice", "created_at": "2026-10-07T11:00:00Z", "cooked": "<p>still</p>"},
                    {"username": "ovh", "created_at": "2026-10-07T13:30:00Z", "cooked": "<p>fixed</p>", "staff": True},
                ]
            },
        }
    )
    router.get(url__regex=r"https://api\.github\.com/repos/ovh/[^/]+/issues\?.*").respond(
        json=[ISSUE, {**ISSUE, "number": 1, "pull_request": {}}]
    )
    router.post("https://www.reddit.com/api/v1/access_token").respond(json={"access_token": "tok"})
    router.get(url__regex=r"https://oauth\.reddit\.com/r/ovh/new.*").respond(json=REDDIT)


@respx.mock
async def test_collects_every_channel(reddit_credentials):
    mock_all(respx)
    collector = Collector(github_token="t")
    threads = await collector.threads()
    sources = sorted(t.source for t in threads)
    assert sources == ["forum", "github", "github", "reddit", "roadmap"]
    forum = next(t for t in threads if t.source == "forum")
    assert forum.excerpt == "GET & PUT fail" and forum.labels == ["incident"] and forum.answered
    assert forum.url == f"{FORUM}/t/s3-503/54094" and "object_storage" in forum.products
    issue = next(t for t in threads if t.source == "github")
    assert issue.reactions == 4 and issue.answered and not issue.solved
    reddit = next(t for t in threads if t.source == "reddit")
    assert reddit.author == "carol" and reddit.replies == 4 and reddit.reactions == 7 and reddit.answered
    assert reddit.labels == ["Public Cloud"] and "kubernetes" in reddit.products
    assert respx.calls[-1].request.headers.get("user-agent", "").startswith("community-radar-mcp")
    oauth = next(c for c in respx.calls if c.request.url.host == "oauth.reddit.com")
    assert oauth.request.headers["authorization"] == "Bearer tok"
    first, staff = await collector.first_response(forum)
    assert first.hour == 13 and staff  # the author's own follow-up does not count
    assert collector.errors == {}


@respx.mock
async def test_reddit_is_off_without_credentials(monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    mock_all(respx)
    collector = Collector(github_token="t")
    threads = await collector.threads()
    assert not collector.reddit_enabled and collector.errors == {}
    assert "reddit" not in {t.source for t in threads}
    assert not any("reddit.com" in str(call.request.url) for call in respx.calls)


@respx.mock
async def test_failing_channel_is_reported_not_fatal(reddit_credentials):
    mock_all(respx)
    respx.get(url__regex=r"https://oauth\.reddit\.com/r/ovh/new.*").respond(429)
    collector = Collector(github_token="")
    threads = await collector.threads()
    assert "reddit" in collector.errors
    assert any(t.source == "forum" for t in threads)
    respx.get(url__regex=r"https://oauth\.reddit\.com/r/ovh/new.*").respond(json=REDDIT)
    assert any(t.source == "reddit" for t in await collector.threads())  # the failure was not cached
    assert "reddit" not in collector.errors


@respx.mock
async def test_github_rate_limit_message():
    respx.get(url__regex=r"https://api\.github\.com/.*").respond(403, headers={"x-ratelimit-remaining": "0"})
    collector = Collector(github_token="")
    with pytest.raises(RateLimited, match="GITHUB_TOKEN"):
        await collector.issue_conversation("ovh/ovhcloud-cli", 300)
    assert await collector.roadmap() == [] and "roadmap" in collector.errors


@respx.mock
async def test_issue_conversation_flags_maintainers():
    respx.get("https://api.github.com/repos/ovh/ovhcloud-cli/issues/300").respond(json=ISSUE)
    respx.get(url__regex=r".*/issues/300/comments.*").respond(
        json=[
            {
                "user": {"login": "rbeuque74"},
                "created_at": "2026-10-20T09:00:00Z",
                "body": "On it",
                "author_association": "MEMBER",
            }
        ]
    )
    thread, posts = await Collector(github_token="t").issue_conversation("ovh/ovhcloud-cli", 300)
    assert thread.source == "github" and [p.staff for p in posts] == [False, True]
    assert respx.calls[0].request.headers["authorization"] == "Bearer t"


def test_html_to_text():
    assert html_to_text("<p>Hello&nbsp;<b>world</b></p><p>Bye</p>") == "Hello world\nBye"


async def test_cache_reuses_results():
    from community_radar.sources import TTLCache

    calls = []

    async def fetch():
        calls.append(1)
        return len(calls)

    cache = TTLCache(60)
    assert await cache.get("k", fetch) == 1
    assert await cache.get("k", fetch) == 1
    assert await cache.get("k", fetch, refresh=True) == 2
    assert await cache.get("x", fetch, keep=lambda v: False) == 3
    assert await cache.get("x", fetch) == 4  # a rejected value is not cached
    _ = httpx  # keep import for respx side effects
