from datetime import UTC, datetime

import pytest
import respx
from starlette.testclient import TestClient

from community_radar.cli import add_routes
from community_radar.config import SourceConfig
from community_radar.docs import DocsIndex
from community_radar.models import Thread
from community_radar.products import classify, in_scope, resolve_product, use_taxonomy
from community_radar.profiles import CLOUD, GENERIC, OVHCLOUD, detect, resolve
from community_radar.report import render_html, snapshot
from community_radar.server import create_server
from community_radar.sources import Collector

NOW = datetime.now(UTC)
FORUM = "https://forum.example.org/community"
TOPIC = {
    "id": 7,
    "title": "Webhook retries never stop",
    "slug": "webhook-retries",
    "created_at": "2026-11-04T10:00:00Z",
    "last_posted_at": "2026-11-04T12:00:00Z",
    "excerpt": "Our endpoint returns 200 but retries continue",
    "reply_count": 0,
    "posts_count": 1,
    "tags": [{"name": "webhooks", "slug": "webhooks"}, {"name": "bug", "slug": "bug"}],
}


@pytest.fixture
def neutral(monkeypatch):
    monkeypatch.delenv("RADAR_PROFILE", raising=False)
    use_taxonomy(GENERIC.taxonomy)


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"github": [{"repo": "ovh/terraform-provider-ovh"}]}, OVHCLOUD),
        ({"forums": [{"url": "https://community.ovhcloud.com"}]}, OVHCLOUD),
        ({"forums": [], "reddit": [{"subreddit": "ovh"}]}, OVHCLOUD),
        ({"forums": [], "github": [{"repo": "grafana/grafana"}], "reddit": [{"subreddit": "selfhosted"}]}, GENERIC),
        ({"forums": [], "reddit": [{"subreddit": "ovhfans"}]}, GENERIC),
    ],
)
def test_profile_is_detected_from_the_sources(config, expected):
    assert detect(SourceConfig.parse(config)) is expected


def test_chosen_profile_wins_over_detection():
    config = SourceConfig.parse({"profile": "cloud", "forums": [], "github": [{"repo": "ovh/ovhcloud-cli"}]})
    assert resolve(config) is CLOUD
    with pytest.raises(ValueError, match="profile must be"):
        SourceConfig.parse({"profile": "aws"})


def test_neutral_profile_uses_platform_tags_as_topics(neutral):
    t = classify(Thread("forum", "1", "Webhook retries never stop", "https://x", NOW, NOW, labels=["webhooks", "bug"]))
    assert t.products == ["webhooks"] and "bug" in t.signals  # "bug" is a kind of thread, not a topic
    assert in_scope(t) and resolve_product("payments") == "payments"


def test_cloud_profile_knows_cloud_families_without_any_provider():
    use_taxonomy(CLOUD.taxonomy)
    t = classify(Thread("reddit", "1", "EKS node group stuck after upgrade", "https://x", NOW, NOW))
    assert t.products == ["kubernetes"] and CLOUD.vendor == "" and CLOUD.title == "Cloud Community Radar"


def test_defaults_are_neutral_without_a_profile(neutral):
    config = SourceConfig.defaults()
    assert (config.profile, config.forums, config.github, config.roadmap) == ("auto", [], [], "")


@respx.mock
async def test_a_neutral_radar_never_mentions_ovhcloud(neutral):
    listing = respx.get(f"{FORUM}/latest.json").respond(json={"topic_list": {"topics": [TOPIC]}})
    respx.get(url__regex=r"https://api\.github\.com/repos/acme/app/issues.*").respond(json=[])
    post = {"username": "dana", "created_at": "2026-11-04T10:00:00Z", "cooked": "<p>Retries continue</p>"}
    respx.get(f"{FORUM}/t/7.json").respond(json={"title": TOPIC["title"], "post_stream": {"posts": [post]}})
    collector = Collector(github_token="t")
    collector.save_sources({"forums": [{"url": FORUM + "/", "label": "Acme forum"}], "github": [{"repo": "acme/app"}]})
    assert collector.profile is GENERIC
    [thread] = await collector.threads()
    assert listing.called and thread.url == f"{FORUM}/t/webhook-retries/7" and thread.channel == "Acme forum"
    assert collector.forum_topic_ref(thread.url) == (FORUM, "7")  # forum served under a sub-path
    page = render_html(await snapshot(collector, DocsIndex([]), 7))
    assert "<h1>Community Radar" in page and "ovh" not in page.lower()
    assert "Content gaps" not in page and "Most wanted on the roadmap" not in page  # no docs, no roadmap


def test_ovhcloud_profile_title_and_vendor_note(collector, docs):
    server = create_server(collector, docs)
    add_routes(server, collector, docs)
    client = TestClient(server.streamable_http_app())
    report = client.get("/report?days=7").text
    assert "<h1>OVHcloud Public Cloud Community Radar" in report and "not affiliated with OVHcloud" in report
    sources = client.get("/sources").text
    assert "Community profile" in sources and "Forums (Discourse)" in sources and '"detected": "ovhcloud"' in sources
