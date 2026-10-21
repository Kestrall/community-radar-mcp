from community_radar import analytics
from tests.conftest import THREADS, thread


async def test_pulse_counts_and_compares_periods(collector):
    p = await analytics.pulse(collector, THREADS, 7, None)
    assert p.total == 7
    assert p.previous_new_threads == 0
    assert p.by_source["forum"] == 3
    assert p.unanswered == 3  # two forum topics and one GitHub issue, roadmap excluded
    assert p.median_first_response_hours == 2.0
    assert p.staff_reply_rate == 1.0
    assert p.top_threads[0].title.startswith("Managed Kubernetes")
    k8s = await analytics.pulse(collector, THREADS, 30, "kubernetes")
    assert {t.title for t in k8s.top_threads} >= {"Old kubernetes question"}


def test_unanswered_excludes_roadmap_reddit_and_solved():
    pending = analytics.unanswered(THREADS, 30, None)
    assert {t.source for t in pending} <= {"forum", "github"}
    assert all(t.answered is False for t in pending)
    assert pending[0].title.startswith("Can't create security groups")  # most viewed for its age


def test_irritants_rank_negative_signals_first():
    top = analytics.irritants(THREADS, 7)
    assert top[0].negative_threads >= 1
    assert all(i.score >= j.score for i, j in zip(top, top[1:], strict=False))
    assert any(i.label == "Object Storage" for i in top)


def test_content_gaps_flags_questions_without_matching_guide(docs):
    gaps = analytics.content_gaps(THREADS, docs, 30, min_score=4)  # tiny test index: lower scores
    titles = [g.thread.title for g in gaps]
    assert "How to mount a block storage volume?" in titles
    assert "Can't create security groups on my instance" not in titles  # a guide covers it


def test_search_threads_prefers_title_matches():
    found = analytics.search_threads(THREADS, "kubernetes autoscaling")
    assert found[0].title == "Managed Kubernetes: Karpenter autoscaler"
    assert analytics.search_threads(THREADS, "the a of") == []
    by_votes = analytics.search_threads(
        [thread(title="kube one", reactions=1), thread(title="kube two", reactions=9)], "kube", by_votes=True
    )
    assert by_votes[0].title == "kube two"
