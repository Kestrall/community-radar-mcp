from datetime import UTC, datetime, timedelta

import pytest
from mcp import Client
from starlette.testclient import TestClient

from community_radar.cli import add_routes
from community_radar.queue_page import render_queue
from community_radar.server import create_server
from community_radar.triage import TriageStore, build_queue
from tests.conftest import THREADS, thread

NOW = datetime.now(UTC)


def test_status_changes_are_logged(tmp_path):
    path = str(tmp_path / "triage.sqlite")
    store = TriageStore(path)
    t = THREADS[0]
    store.update(t, "in_progress", "Reproduced on GRA", assignee="Thomas", actor="Thomas")
    store.update(t, "escalated", "Sent to the storage team", team="product", actor="Thomas")
    reloaded = TriageStore(path)
    ticket = reloaded.tickets()[t.url]
    assert (ticket.status, ticket.team, ticket.assignee) == ("escalated", "product", None)
    history = reloaded.history(t.url)
    assert [(e.from_status, e.to_status) for e in history] == [("new", "in_progress"), ("in_progress", "escalated")]
    assert history[0].note == "Reproduced on GRA" and history[1].actor == "Thomas"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"status": "done"}, "status must be one of"),
        ({"status": "escalated"}, "needs a team"),
        ({"status": "escalated", "team": "marketing"}, "team must be one of"),
        ({"status": "dismissed"}, "needs a note"),
    ],
)
def test_rejects_incomplete_decisions(kwargs, message):
    with pytest.raises(ValueError, match=message):
        TriageStore().update(THREADS[0], **kwargs)


def test_new_community_activity_brings_a_handled_thread_back_on_top():
    store = TriageStore()
    quiet, revived, untouched = (
        thread(title="Quiet", id="q", days=3),
        thread(title="Revived", id="r", days=3),
        thread(title="Untouched", id="u", days=1),
    )
    store.update(quiet, "answered")
    store.update(revived, "waiting_user")
    revived.last_activity = NOW + timedelta(minutes=5)  # the user wrote after our status change
    queue = build_queue([quiet, revived, untouched], store, {untouched.url: ("no_response", "no response")})
    assert [i.thread.title for i in queue.items] == ["Revived", "Untouched", "Quiet"]
    assert queue.items[0].new_activity and not queue.items[2].new_activity
    assert queue.new_activity == 1 and queue.counts == {"waiting_user": 1, "new": 1, "answered": 1}
    assert queue.items[1].hint == "no response"


def test_queue_page_has_platform_and_status_filters():
    store = TriageStore()
    store.update(THREADS[0], "escalated", team="engineering")
    page = render_queue(build_queue(THREADS, store), 30)
    assert 'data-k=source data-v="github"' in page and 'data-k=status data-v="escalated"' in page
    assert "→ Engineering" in page and "X-Radar-Triage" in page


def test_triage_routes(collector, docs):
    server = create_server(collector, docs)
    add_routes(server, collector, docs)
    client = TestClient(server.streamable_http_app())
    url = THREADS[0].url
    headers = {"X-Radar-Triage": "1"}
    assert client.post("/api/triage", json={"url": url, "status": "answered"}).status_code == 400  # no header
    assert client.post("/api/triage", json={"url": "https://x", "status": "new"}, headers=headers).status_code == 404
    bad = client.post("/api/triage", json={"url": url, "status": "dismissed"}, headers=headers)
    assert bad.status_code == 400 and "note" in bad.json()["error"]
    ok = client.post("/api/triage", json={"url": url, "status": "answered", "actor": "Thomas"}, headers=headers)
    assert ok.json()["status"] == "answered"
    assert client.get("/api/triage/history", params={"url": url}).json()[0]["actor"] == "Thomas"
    page = client.get("/triage?days=30").text
    assert "Triage queue" in page and 'class="on">Triage' in page
    report = client.get("/report?days=7").text
    assert 'href="/report?days=7&source=github"' in report and 'href="/triage?days=30"' in report


async def test_triage_tools(collector, docs):
    async with Client(create_server(collector, docs)) as client:
        url = THREADS[3].url
        update = await client.call_tool("update_triage", {"url": url, "status": "escalated", "team": "engineering"})
        assert not update.is_error and update.structured_content["history"][0]["actor"] == "assistant (MCP)"
        missing_team = await client.call_tool("update_triage", {"url": url, "status": "escalated"})
        assert missing_team.is_error
        queue = (await client.call_tool("triage_queue", {"status": "escalated"})).structured_content
        assert [i["thread"]["url"] for i in queue["items"]] == [url] and queue["items"][0]["team"] == "engineering"
        open_items = (await client.call_tool("triage_queue", {"status": "open", "source": "github"})).structured_content
        assert all(i["status"] in ("new", "in_progress") for i in open_items["items"])


def test_to_handle_leaves_out_roadmap_wishes_and_maintainer_issues():
    wish = thread(source="roadmap", title="Wish", id="w")
    internal = thread(source="github", title="Internal", id="i", staff_author=True)
    flagged = thread(source="roadmap", title="Shipped, nobody told", id="f")
    question = thread(title="Question", id="q")
    queue = build_queue(
        [wish, internal, flagged, question], TriageStore(), {flagged.url: ("fixed_untold", "tell them")}
    )
    assert {i.thread.title: i.needs_action for i in queue.items} == {
        "Wish": False,
        "Internal": False,
        "Shipped, nobody told": True,
        "Question": True,
    }
    assert queue.to_handle == 2
