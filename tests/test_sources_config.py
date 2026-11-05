import json
from datetime import UTC, datetime

import pytest
import respx
from starlette.testclient import TestClient

from community_radar.cli import add_routes
from community_radar.config import LIMIT, SourceConfig, SourceStore
from community_radar.server import create_server
from community_radar.sources import Collector

NOW = datetime.now(UTC)
CONFIG = {
    "roadmap": "ovh/public-cloud-roadmap",
    "github": [
        {"repo": "https://github.com/ovh/python-ovh/", "enabled": True},
        {"repo": "ovh/go-ovh", "enabled": False},
    ],
    "discord": [{"guild": "1567232781166055444", "label": "Demo", "channels": "cloud-help", "demo": True}],
    "reddit": [{"subreddit": "r/ovh"}, {"subreddit": "selfhosted", "enabled": False}],
    "feeds": [{"url": "https://blog.example.com/feed.xml", "label": "Cloud blog"}],
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("DISCORD_GUILD_ID", "DISCORD_BOT_TOKEN", "REDDIT_CLIENT_ID", "RADAR_SOURCES_PATH", "RADAR_INBOX_PATH"):
        monkeypatch.delenv(name, raising=False)


def test_parse_normalises_and_keeps_enabled_entries():
    config = SourceConfig.parse(CONFIG)
    assert config.repos() == ["ovh/python-ovh"]  # URL trimmed, disabled repo left out
    assert config.subreddits() == ["ovh"]
    assert config.guilds()[0]["demo"] is True and config.guilds()[0]["enabled"] is True
    assert config.imported_feeds() == [
        {"source": "reddit", "channel": "r/ovh", "url": "https://www.reddit.com/r/ovh/new/.rss?limit=100"},
        {"source": "rss", "channel": "Cloud blog", "url": "https://blog.example.com/feed.xml"},
    ]
    assert config.allows_import("reddit", "r/ovh") and not config.allows_import("reddit", "r/selfhosted")
    assert config.allows_import("stackoverflow", "anything")  # channels pushed by other collectors stay visible


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"github": [{"repo": "not a repo"}]}, "owner/repo"),
        ({"github": [{"repo": "ovh/a"}, {"repo": "OVH/A"}]}, "listed twice"),
        ({"github": [{"repo": f"ovh/r{n}"} for n in range(LIMIT + 1)]}, f"at most {LIMIT}"),
        ({"discord": [{"guild": "abc"}]}, "server id is a number"),
        ({"reddit": [{"subreddit": "bad name!"}]}, "not a subreddit"),
        ({"feeds": [{"url": "http://insecure.example.com/rss"}]}, "https feed URL"),
        ({"roadmap": "nope"}, "roadmap"),
    ],
)
def test_parse_rejects_bad_entries(change, message):
    with pytest.raises(ValueError, match=message):
        SourceConfig.parse({**CONFIG, **change})


def test_defaults_come_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("DISCORD_GUILD_ID", "1567232781166055444")
    monkeypatch.setenv("DISCORD_DEMO", "true")
    store = SourceStore(str(tmp_path / "sources.json"))
    assert store.config.repos() == ["ovh/terraform-provider-ovh", "ovh/ovhcloud-cli"]
    assert store.config.guilds()[0]["demo"] and store.config.subreddits() == ["ovh"]
    store.save(CONFIG)
    assert SourceStore(str(tmp_path / "sources.json")).config.repos() == ["ovh/python-ovh"]  # saved config wins


@respx.mock
async def test_collector_follows_the_configuration(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "bot")
    respx.get(url__regex=r"https://community\.ovhcloud\.com/.*").respond(json={"topic_list": {"topics": []}})
    issues = respx.get(url__regex=r"https://api\.github\.com/repos/.*/issues.*").respond(json=[])
    collector = Collector(github_token="t")
    collector.save_sources({**CONFIG, "discord": []})
    await collector.threads()
    called = {c.request.url.path for c in issues.calls}
    assert called == {"/repos/ovh/python-ovh/issues", "/repos/ovh/public-cloud-roadmap/issues"}
    assert collector.discords == {} and not collector.discord_enabled
    collector.save_sources(CONFIG)
    assert list(collector.discords) == ["1567232781166055444"] and collector.discords["1567232781166055444"].demo
    collector.inbox.ingest(
        {
            "source": "reddit",
            "channel": "r/selfhosted",
            "items": [{"id": "1", "title": "Public Cloud?", "url": "https://x.io/1", "created": NOW.isoformat()}],
        }
    )
    assert collector.config.allows_import("reddit", "r/selfhosted") is False  # disabled subreddit stays hidden


def test_sources_page_and_api(collector, docs, monkeypatch):
    server = create_server(collector, docs)
    add_routes(server, collector, docs)
    client = TestClient(server.streamable_http_app())
    page = client.get("/sources").text
    assert "Sources" in page and 'class="on">Sources' in page and "+ Add" in page
    assert client.post("/api/sources", json=CONFIG).status_code == 400  # header required
    bad = client.post("/api/sources", json={"github": [{"repo": "x"}]}, headers={"X-Radar-Triage": "1"})
    assert bad.status_code == 400 and "owner/repo" in bad.json()["error"]
    ok = client.post("/api/sources", json=CONFIG, headers={"X-Radar-Triage": "1"})
    assert ok.status_code == 200 and client.get("/api/sources").json()["github"][0]["repo"] == "ovh/python-ovh"
    assert client.get("/api/sources/feeds").status_code == 401
    monkeypatch.setenv("RADAR_INGEST_TOKEN", "s3cret")
    feeds = client.get("/api/sources/feeds", headers={"Authorization": "Bearer s3cret"}).json()["feeds"]
    assert [f["channel"] for f in feeds] == ["r/ovh", "Cloud blog"]
    assert json.loads(json.dumps(feeds))  # plain JSON for n8n
