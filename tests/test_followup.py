from datetime import timedelta

from community_radar.followup import assess_forum, assess_issue, follow_ups
from community_radar.sources import Post
from tests.conftest import NOW, iso, thread

MAINTAINER = {"login": "maintainer"}


def issue(days=30, **kw):
    kw.setdefault("author", "reporter")
    return thread(source="github", title="[BUG] kubeconfig not updated", days=days, channel="ovh/repo", **kw)


def comment(days_ago, staff, login=None):
    return {
        "event": "commented",
        "user": {"login": login or ("maintainer" if staff else "reporter")},
        "author_association": "MEMBER" if staff else "NONE",
        "created_at": iso(NOW - timedelta(days=days_ago)),
    }


def pr(days_ago, number=1552, merged=False, state="open"):
    merged_at = iso(NOW - timedelta(days=days_ago)) if merged else None
    return {
        "event": "cross-referenced",
        "actor": MAINTAINER,
        "created_at": iso(NOW - timedelta(days=days_ago)),
        "source": {
            "issue": {
                "number": number,
                "title": "fix",
                "state": "closed" if merged else state,
                "html_url": f"https://github.com/ovh/repo/pull/{number}",
                "pull_request": {"merged_at": merged_at},
            }
        },
    }


def event(kind, days_ago, actor=MAINTAINER, **extra):
    return {"event": kind, "actor": actor, "created_at": iso(NOW - timedelta(days=days_ago)), **extra}


def test_silent_progress_when_work_happens_after_the_last_word():
    events = [
        event("labeled", 20),
        event("assigned", 1, assignee=MAINTAINER),
        event("milestoned", 1, milestone={"title": "v2.24"}),
        pr(1),
    ]
    f = assess_issue(issue(194), events, NOW)
    assert f.status == "silent_progress" and f.milestone == "v2.24" and f.assignees == ["maintainer"]
    assert "PR #1552 open" in f.reason and "no maintainer comment since it was opened, 194 days ago" in f.reason
    assert f.days_waiting(NOW) == 194  # the reporter has heard nothing since the issue was opened
    assert assess_issue(issue(194), [*events, comment(0, staff=True)], NOW) is None  # someone told them


def test_merged_fix_nobody_announced():
    f = assess_issue(issue(), [comment(10, staff=True), pr(3, merged=True)], NOW)
    assert f.status == "fixed_untold" and f.prs[0].state == "merged" and "PR #1552" in f.reason
    assert assess_issue(issue(), [pr(3, merged=True), comment(2, staff=True)], NOW) is None


def test_silences_and_conversations_waiting_on_us():
    assert assess_issue(issue(9, replies=2), [comment(5, staff=False, login="other")], NOW).status == "no_response"
    labelled = assess_issue(issue(9), [event("labeled", 8)], NOW)
    assert labelled.status == "triaged_silently" and "labelled by maintainers" in labelled.reason
    waiting = assess_issue(issue(), [comment(10, staff=True), comment(4, staff=False)], NOW)
    assert waiting.status == "waiting_on_us" and waiting.days_waiting(NOW) == 4
    assert assess_issue(issue(), [comment(10, staff=False), comment(4, staff=True)], NOW) is None
    assert assess_issue(issue(1), [], NOW) is None  # too recent to be a silence
    assert assess_issue(issue(1), [], NOW, min_days=0).status == "no_response"
    # The reporter labelling their own issue, or a bot commenting, is not a maintainer reaction.
    bot = {"event": "commented", "user": {"login": "stale[bot]", "type": "Bot"}, "created_at": iso(NOW)}
    own = event("labeled", 5, actor={"login": "reporter"})
    assert assess_issue(issue(9), [own, bot], NOW).status == "no_response"


def test_closed_issues_and_internal_work():
    closed = assess_issue(issue(solved=True), [event("labeled", 9), event("closed", 2)], NOW)
    assert closed.status == "closed_silently" and "no fix linked" in closed.reason
    # Closed by a merged pull request: GitHub shows the fix to the reporter, nothing to add.
    assert assess_issue(issue(solved=True), [pr(2, merged=True), event("closed", 2)], NOW) is None
    assert assess_issue(issue(solved=True), [event("closed", 2, commit_id="abc")], NOW) is None
    by_author = event("closed", 2, actor={"login": "reporter"})
    assert assess_issue(issue(solved=True), [by_author], NOW) is None
    assert assess_issue(issue(solved=True), [comment(3, staff=True), event("closed", 2)], NOW) is None
    # A maintainer with a private org membership comments as CONTRIBUTOR: their closing comment counts.
    private = {**comment(2, staff=False, login="maintainer"), "author_association": "CONTRIBUTOR"}
    assert assess_issue(issue(solved=True), [private, event("closed", 2)], NOW) is None
    assert assess_issue(issue(staff_author=True), [], NOW) is None  # an internal EPIC is not a community question
    reopened = [event("closed", 5), event("reopened", 4)]
    assert assess_issue(issue(9), reopened, NOW).status == "triaged_silently"


def test_forum_topics():
    topic = thread(title="S3 503", days=6)
    q = Post("alice", NOW - timedelta(days=6), "503")
    assert assess_forum(topic, [q], NOW).status == "no_response"
    peer = Post("bob", NOW - timedelta(days=5), "try this")
    assert assess_forum(topic, [q, peer], NOW).status == "community_answered"
    staff = Post("ovh", NOW - timedelta(days=5), "fixed", staff=True)
    again = Post("alice", NOW - timedelta(days=3), "still failing")
    assert assess_forum(topic, [q, staff, again], NOW).status == "waiting_on_us"
    assert assess_forum(topic, [q, staff], NOW) is None
    assert assess_forum(thread(title="done", solved=True), [q], NOW) is None


async def test_follow_ups_ranks_news_first_and_survives_rate_limits(collector):
    from community_radar.sources import RateLimited

    found = await follow_ups(collector, await collector.threads(), 30, min_days=0)
    assert found[0].status == "silent_progress" and found[0].thread.id == "ovh/terraform-provider-ovh#1"
    assert {f.thread.source for f in found} <= {"forum", "github"}

    async def limited(repo, number):
        raise RateLimited("GitHub API rate limit reached")

    collector.issue_timeline = limited
    found = await follow_ups(collector, await collector.threads(), 30, min_days=0)
    assert "timelines" in collector.errors and all(f.thread.source == "forum" for f in found)
