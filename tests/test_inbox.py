from datetime import UTC, datetime, timedelta

import pytest
import respx
from starlette.testclient import TestClient

from community_radar.cli import add_routes
from community_radar.inbox import Inbox
from community_radar.server import create_server
from community_radar.sources import Collector

NOW = datetime.now(UTC)


def batch(**item):
    base = {
        "id": "t3_abc",
        "title": "Managed Kubernetes cluster stuck after upgrade?",
        "url": "https://www.reddit.com/r/ovh/comments/abc/",
        "created": (NOW - timedelta(hours=5)).isoformat(),
        "author": "/u/someone",
        "text": "My node pool never becomes Ready since the upgrade.",
    }
    return {"source": "reddit", "channel": "r/ovh", "items": [{**base, **item}]}


def test_ingest_classifies_deduplicates_and_persists(tmp_path):
    path = tmp_path / "inbox.json"
    inbox = Inbox(str(path))
    assert inbox.ingest(batch()) == {"received": 1, "added": 1, "kept": 1}
    assert inbox.ingest(batch(title="Managed Kubernetes cluster stuck after upgrade? (edited)"))["added"] == 0
    [t] = Inbox(str(path)).threads()  # reloaded from disk
    assert t.source == "reddit" and t.id == "reddit:t3_abc" and t.channel == "r/ovh"
    assert t.title.endswith("(edited)") and "kubernetes" in t.products and "question" in t.signals
    assert t.answered is None  # an RSS feed does not say whether anyone replied
    assert Inbox().ingest(batch(replies=3)) and Inbox().channels() == {}


def test_old_threads_expire():
    inbox = Inbox()
    inbox.ingest(batch(created=(NOW - timedelta(days=200)).isoformat()))
    assert inbox.threads() == []


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "JSON object"),
        ({**batch(), "source": "Forum!"}, "source"),
        ({**batch(), "source": "github"}, "collected by the radar itself"),
        (batch(url="javascript:alert(1)"), "https url"),
        (batch(created="yesterday"), "Invalid isoformat"),
        ({**batch(), "items": [{}] * 501}, "at most 500"),
    ],
)
def test_rejects_bad_payloads(payload, message):
    with pytest.raises(ValueError, match=message):
        Inbox().ingest(payload)


def test_ingest_route_needs_the_token(collector, docs, monkeypatch):
    server = create_server(collector, docs)
    add_routes(server, collector, docs)
    client = TestClient(server.streamable_http_app())
    assert client.post("/ingest", json=batch()).status_code == 404  # off without RADAR_INGEST_TOKEN
    monkeypatch.setenv("RADAR_INGEST_TOKEN", "s3cret")
    assert client.post("/ingest", json=batch(), headers={"Authorization": "Bearer nope"}).status_code == 401
    bad = client.post("/ingest", json={"source": "reddit"}, headers={"Authorization": "Bearer s3cret"})
    assert bad.status_code == 400 and "items" in bad.json()["error"]
    ok = client.post("/ingest", json=batch(), headers={"Authorization": "Bearer s3cret"})
    assert ok.json() == {"received": 1, "added": 1, "kept": 1}
    assert "r/ovh (imported)" in client.get("/report?days=7").text


@respx.mock
async def test_imported_threads_join_the_collected_ones(monkeypatch):
    for name in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "DISCORD_BOT_TOKEN", "RADAR_INBOX_PATH"):
        monkeypatch.delenv(name, raising=False)
    respx.get(url__regex=r"https://community\.ovhcloud\.com/.*").respond(json={"topic_list": {"topics": []}})
    respx.get(url__regex=r"https://api\.github\.com/.*").respond(json=[])
    collector = Collector(github_token="t")
    collector.inbox.ingest(batch(id="t3_new"))
    assert [t.id for t in await collector.threads()] == ["reddit:t3_new"]
    # With approved API credentials the live Reddit channel wins: the import is not shown twice.
    monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
    respx.post("https://www.reddit.com/api/v1/access_token").respond(json={"access_token": "tok"})
    respx.get(url__regex=r"https://oauth\.reddit\.com/.*").respond(json={"data": {"children": []}})
    live = Collector(github_token="t")
    live.inbox = collector.inbox
    assert await live.threads() == []


@pytest.mark.parametrize(
    ("title", "text", "kept"),
    [
        ("Does the Public Cloud Free Trial credit cover AI Endpoints?", "", True),
        ("Internet stuck inside my instance in SGP", "", True),
        ("MKS cluster upgrade failed", "node pool stuck", True),
        ("No VPS available?", "medium 4cpu VPS in Quebec", False),
        ("Heads-up: .MS domain operations failing", "renewal fails", False),
        ("I got auto-invoiced for a server never used", "payment blocked", False),
        ("No support of DKIM improvements", "domain registration and dns", False),
    ],
)
def test_only_public_cloud_threads_are_imported(title, text, kept):
    from community_radar.models import Thread
    from community_radar.products import classify, in_public_cloud

    assert in_public_cloud(classify(Thread("reddit", "x", title, "https://x", NOW, NOW, text))) is kept
