from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from community_radar import followup
from community_radar.discord_source import API, DiscordSource, snowflake_time, time_snowflake
from community_radar.sources import Collector

GUILD = "123456789012345678"
NOW = datetime.now(UTC)


def sid(hours_ago: float, n: int = 0) -> str:
    """A Discord id created `hours_ago` hours ago (n separates ids created at the same time)."""
    return str(int(time_snowflake(NOW - timedelta(hours=hours_ago))) + n)


def user(uid: str, name: str, bot: bool = False) -> dict:
    return {"id": uid, "username": name, "global_name": name.title(), "bot": bot}


ALICE, BOB, STAFF = user("1", "alice"), user("2", "bob"), user("3", "ovh_maxime")
FORUM_ID, TEXT_ID = "10", "20"
POST_ID = sid(30)  # a forum post: thread id == starter message id
QUESTION_ID = sid(5)
CHANNELS = [
    {
        "id": FORUM_ID,
        "type": 15,
        "name": "public-cloud-help",
        "available_tags": [{"id": "t1", "name": "Kubernetes"}, {"id": "t2", "name": "Solved"}],
    },
    {"id": TEXT_ID, "type": 0, "name": "general"},
    {"id": "30", "type": 2, "name": "voice"},
]
FORUM_THREAD = {
    "id": POST_ID,
    "type": 11,
    "parent_id": FORUM_ID,
    "name": "MKS node pool stuck in REDEPLOYING",
    "message_count": 2,
    "applied_tags": ["t1"],
    "last_message_id": sid(2),
    "thread_metadata": {"archived": False, "create_timestamp": (NOW - timedelta(hours=30)).isoformat()},
}
ARCHIVED = {
    **FORUM_THREAD,
    "id": sid(24 * 90),
    "name": "Old S3 question",
    "applied_tags": ["t2"],
    "last_message_id": sid(24 * 80),
}
TEXT_MESSAGES = [  # newest first, as Discord returns them
    {"id": sid(1), "type": 19, "author": ALICE, "content": "thanks!", "message_reference": {"message_id": sid(3)}},
    {"id": sid(3), "type": 19, "author": BOB, "content": "Try openstack server rebuild", "message_reference": {"message_id": QUESTION_ID}},
    {"id": sid(4), "type": 0, "author": user("9", "ci", bot=True), "content": "Deployment finished on every region today"},
    {"id": sid(4.5), "type": 0, "author": BOB, "content": "hello"},
    {"id": QUESTION_ID, "type": 0, "author": ALICE, "content": "My Public Cloud instance does not boot after resize, any idea?", "reactions": [{"count": 3}]},
]  # fmt: skip


@pytest.fixture
def discord_env(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("DISCORD_GUILD_ID", GUILD)


def mock_discord(router):
    router.get(f"{API}/guilds/{GUILD}").respond(json={"id": GUILD, "name": "Radar Démo"})
    router.get(f"{API}/guilds/{GUILD}/channels").respond(json=CHANNELS)
    router.get(f"{API}/guilds/{GUILD}/roles").respond(
        json=[{"id": "r1", "name": "OVHcloud Staff"}, {"id": "r2", "name": "Member"}]
    )
    router.get(f"{API}/guilds/{GUILD}/members/3").respond(json={"roles": ["r1"]})
    router.get(url__regex=rf"{API}/guilds/{GUILD}/members/[12]$").respond(json={"roles": ["r2"]})
    router.get(f"{API}/guilds/{GUILD}/threads/active").respond(
        json={"threads": [FORUM_THREAD, {**FORUM_THREAD, "id": "77", "parent_id": "999"}]}
    )
    router.get(url__regex=rf"{API}/channels/{FORUM_ID}/threads/archived/public.*").respond(
        json={"threads": [ARCHIVED], "has_more": False}
    )
    router.get(f"{API}/channels/{POST_ID}/messages/{POST_ID}").respond(
        json={
            "id": POST_ID,
            "type": 0,
            "author": ALICE,
            "content": "Since the upgrade my node pool never finishes",
            "reactions": [{"count": 2}],
        }
    )
    router.get(f"{API}/channels/{ARCHIVED['id']}/messages/{ARCHIVED['id']}").respond(status_code=404)
    router.get(url__regex=rf"{API}/channels/{TEXT_ID}/messages\?.*").respond(json=TEXT_MESSAGES)
    router.get(url__regex=rf"{API}/channels/{POST_ID}/messages\?.*").respond(
        json=[
            {"id": sid(2), "type": 0, "author": ALICE, "content": "Any news?"},
            {"id": sid(20), "type": 0, "author": STAFF, "content": "We are looking into it"},
            {"id": POST_ID, "type": 0, "author": ALICE, "content": "Since the upgrade my node pool never finishes"},
        ]
    )  # fmt: skip


def test_snowflakes_round_trip():
    moment = datetime(2026, 10, 1, 12, tzinfo=UTC)
    assert snowflake_time(time_snowflake(moment)) == moment


def test_off_without_credentials(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
    assert not Collector(github_token="").discord_enabled


@respx.mock
async def test_reads_forum_posts_and_text_channel_questions(discord_env):
    mock_discord(respx)
    source = DiscordSource(httpx.AsyncClient())
    threads = {t.title: t for t in await source.threads(NOW - timedelta(days=30))}
    assert set(threads) == {
        "MKS node pool stuck in REDEPLOYING",
        "My Public Cloud instance does not boot after resize, any idea?",
    }  # archived post too old, other server's thread, bot message and chit-chat left out
    post = threads["MKS node pool stuck in REDEPLOYING"]
    assert post.source == "discord" and post.channel == "discord #public-cloud-help"
    assert post.url == f"https://discord.com/channels/{GUILD}/{POST_ID}" and post.labels == ["Kubernetes"]
    assert post.author == "Alice" and post.reactions == 2 and post.answered and not post.solved
    assert "kubernetes" in post.products
    question = threads["My Public Cloud instance does not boot after resize, any idea?"]
    assert question.id == f"{TEXT_ID}/{QUESTION_ID}" and question.replies == 2 and question.answered
    assert question.reactions == 3 and question.last_activity == snowflake_time(sid(1))
    auth = respx.calls[0].request.headers["authorization"]
    assert auth == "Bot bot-token"


@respx.mock
async def test_solved_tag_and_archived_posts(discord_env):
    mock_discord(respx)
    source = DiscordSource(httpx.AsyncClient())
    old = next(t for t in await source.threads(NOW - timedelta(days=100)) if t.title == "Old S3 question")
    assert old.solved and old.labels == ["Solved"] and old.excerpt == ""  # starter message deleted


@respx.mock
async def test_conversation_flags_staff_and_feeds_follow_ups(discord_env):
    mock_discord(respx)
    collector = Collector(github_token="")
    posts = await collector.discord_topic(f"{POST_ID}/{POST_ID}", f"https://discord.com/channels/{GUILD}/{POST_ID}")
    assert [(p.author, p.staff) for p in posts] == [("Alice", False), ("Ovh_Maxime", True), ("Alice", False)]
    thread = next(
        t for t in await collector.discords[GUILD].threads(NOW - timedelta(days=30)) if t.id.startswith(POST_ID)
    )
    status = followup.assess_forum(thread, posts, NOW, min_days=0)
    assert status.status == "waiting_on_us"  # Alice asked again after the staff answer


@respx.mock
async def test_text_question_conversation_follows_the_reply_chain(discord_env):
    mock_discord(respx)
    root = TEXT_MESSAGES[-1]
    respx.get(f"{API}/channels/{TEXT_ID}/messages/{QUESTION_ID}").respond(json=root)
    source = DiscordSource(httpx.AsyncClient())
    messages = await source.conversation(TEXT_ID, QUESTION_ID)
    assert [m["content"] for m in messages] == [root["content"], "Try openstack server rebuild", "thanks!"]


@respx.mock
async def test_waits_when_rate_limited(discord_env, monkeypatch):
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr("community_radar.discord_source.asyncio.sleep", sleep)
    respx.get(f"{API}/guilds/{GUILD}/roles").mock(
        side_effect=[httpx.Response(429, json={"retry_after": 0.5}), httpx.Response(200, json=[])]
    )
    assert await DiscordSource(httpx.AsyncClient()).staff_roles() == set()
    assert waits == [0.5]


@respx.mock
async def test_explains_missing_permissions(discord_env):
    respx.get(f"{API}/guilds/{GUILD}").respond(status_code=403)
    collector = Collector(github_token="")
    respx.get(url__regex=r"https://(community\.ovhcloud\.com|api\.github\.com)/.*").respond(json={})
    await collector.threads()
    assert "Message Content intent" in collector.errors[f"discord:{GUILD}"]


def test_webhooks_only_count_on_demo_servers(discord_env, monkeypatch):
    hook = {"id": "5", "type": 0, "webhook_id": "w", "author": user("w", "Camille", bot=True), "content": "x"}
    assert DiscordSource(httpx.AsyncClient())._automated(hook)
    monkeypatch.setenv("DISCORD_INCLUDE_WEBHOOKS", "true")
    assert not DiscordSource(httpx.AsyncClient())._automated(hook)
    assert DiscordSource(httpx.AsyncClient())._automated({"author": user("9", "ci", bot=True)})


def test_security_groups_are_network_configuration_not_a_security_issue():
    from community_radar.models import Thread
    from community_radar.products import classify

    t = classify(
        Thread("discord", "1/1", "Private network: instances can't ping", "u", NOW, NOW, "Security groups allow ICMP.")
    )
    assert "security" not in t.signals and "feature_request" not in t.signals
    assert "network" in t.products and "iam_security" not in t.products
    leak = classify(Thread("forum", "2", "Security issue: access key leaked", "u", NOW, NOW))
    assert "security" in leak.signals and "iam_security" in leak.products


async def test_report_flags_demo_threads_and_names_the_server(discord_env, monkeypatch):
    from community_radar.docs import DocsIndex
    from community_radar.report import render_html, snapshot
    from tests.conftest import FakeCollector, thread

    collector = FakeCollector(
        threads=[thread(source="discord", title="Instance stuck in rescue", answered=False, demo=True, id="1/1")]
    )
    reader = DiscordSource(httpx.AsyncClient(), token="t", guild=GUILD, demo=True)
    reader.guild_name = "Radar Démo"
    collector.discords = {GUILD: reader}
    page = render_html(await snapshot(collector, DocsIndex([]), 7, "discord"))
    assert "Community Radar · Discord" in page and 'badge demo">demo' in page
    assert "« Radar Démo » (fictional demo data)" in page and "OVHcloud Discord" not in page
    assert "fictional conversations" in page
