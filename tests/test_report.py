from starlette.testclient import TestClient

from community_radar.cli import add_routes
from community_radar.report import render_html, render_telegram, snapshot
from community_radar.server import create_server


async def test_html_report_has_every_section(collector, docs):
    html = render_html(await snapshot(collector, docs, 7))
    for section in ("Top irritants", "Next moves for the community team", "Content gaps", "Most wanted on the roadmap"):
        assert section in html
    assert "Karpenter" in html and "prefers-color-scheme:dark" in html
    assert "in progress, silently" in html and "Post a status update" in html
    assert "<script" not in html


async def test_telegram_digest_fits_in_messages(collector, docs):
    messages = render_telegram(await snapshot(collector, docs, 7))
    assert messages and all(len(m) <= 4096 for m in messages)
    assert "Top irritants" in messages[0]


def test_http_routes(collector, docs):
    server = create_server(collector, docs)
    add_routes(server, collector, docs)
    with TestClient(server.streamable_http_app()) as http:
        assert http.get("/health").json() == {"status": "ok", "guides": 3}
        assert "Community Radar" in http.get("/report?days=14").text
        assert http.get("/digest.json?days=abc").json()["messages"]
