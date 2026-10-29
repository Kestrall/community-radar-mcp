from mcp import Client

from community_radar.server import create_server


def connect(collector, docs):
    return Client(create_server(collector, docs))


async def call(client, name, **args):
    result = await client.call_tool(name, args)
    assert not result.is_error, result.content[0].text
    return result.structured_content


async def test_lists_tools_prompts_and_resources(collector, docs):
    async with connect(collector, docs) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        prompts = {p.name for p in (await client.list_prompts()).prompts}
        sources = (await client.read_resource("radar://sources")).contents[0].text
        taxonomy = (await client.read_resource("radar://taxonomy")).contents[0].text
        report = await client.get_prompt("weekly_report", {"days": "14"})
    assert tools == {
        "community_pulse",
        "find_irritants",
        "unanswered_questions",
        "follow_ups",
        "search_threads",
        "roadmap_status",
        "search_docs",
        "content_gaps",
        "thread_context",
    }
    assert prompts == {"weekly_report", "draft_reply", "tutorial_from_gap"}
    assert "3 Public Cloud guides" in sources and "`kubernetes`" in taxonomy
    assert "find_irritants(days=14)" in report.messages[0].content.text


async def test_tools_return_structured_results(collector, docs):
    async with connect(collector, docs) as client:
        pulse = await call(client, "community_pulse", days=7)
        assert pulse["active_threads"] == 7 and pulse["forum_median_first_response_hours"] == 2.0
        k8s = await call(client, "community_pulse", days=30, product="k8s")
        assert k8s["product"] == "Managed Kubernetes"
        irritants = (await call(client, "find_irritants", days=7))["result"]
        assert irritants and irritants[0]["examples"]
        pending = (await call(client, "unanswered_questions"))["result"]
        assert all(t["answered"] is False for t in pending)
        found = (await call(client, "search_threads", query="security groups", source="forum"))["result"]
        assert found[0]["title"].startswith("Can't create")
        roadmap = await call(client, "roadmap_status", query="karpenter")
        assert roadmap["matches"] == 1 and roadmap["items"][0]["votes"] == 38
        guides = (await call(client, "search_docs", query="security group", lang="fr"))["result"]
        assert guides[0]["url"].startswith("https://help.ovhcloud.com/csm/fr-")
        assert (await call(client, "content_gaps"))["result"]
        moves = (await call(client, "follow_ups", min_days=0))["result"]
        assert moves[0]["status"] == "silent_progress" and moves[0]["linked_pull_requests"][0]["number"] == 1552
        assert moves[0]["suggested_action"].startswith("Post a status update")


async def test_thread_context_for_forum_and_github(collector, docs):
    async with connect(collector, docs) as client:
        forum = await call(client, "thread_context", url="https://community.ovhcloud.com/t/s3-503/54094")
        assert [p["staff"] for p in forum["posts"]] == [False, True]
        assert forum["related_guides"][0]["title"] == "Getting started with Object Storage"
        issue = await call(client, "thread_context", url="https://github.com/ovh/ovhcloud-cli/issues/300")
        assert issue["thread"]["channel"] == "ovh/ovhcloud-cli" and issue["posts"][1]["staff"]
        assert issue["follow_up"] is None  # a maintainer answered last
        assert forum["follow_up"] is None


async def test_errors_are_readable(collector, docs):
    async with connect(collector, docs) as client:
        bad = await client.call_tool("community_pulse", {"product": "banana"})
        assert bad.is_error and "Known products" in bad.content[0].text
        bad = await client.call_tool("thread_context", {"url": "https://example.com/x"})
        assert bad.is_error and "Supported URLs" in bad.content[0].text
    collector._roadmap = []
    collector.errors["roadmap"] = "rate limit"
    async with connect(collector, docs) as client:
        bad = await client.call_tool("roadmap_status", {"query": "x"})
        assert bad.is_error and "rate limit" in bad.content[0].text
