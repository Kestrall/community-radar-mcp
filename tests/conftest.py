from datetime import UTC, datetime, timedelta

import pytest

from community_radar.docs import DocsIndex
from community_radar.models import Guide, Thread
from community_radar.products import classify
from community_radar.sources import Collector, Post

NOW = datetime.now(UTC)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def thread(source="forum", title="Question", days=1, **kw) -> Thread:
    created = NOW - timedelta(days=days)
    kw.setdefault("last_activity", created)
    kw.setdefault("url", f"https://example.com/{source}/{title.replace(' ', '-')}")
    return classify(Thread(source=source, id=kw.pop("id", title), title=title, created=created, **kw))


THREADS = [
    thread(
        title="Object Storage S3 : 503 ServiceUnavailable intermittentes",
        days=2,
        replies=3,
        views=400,
        answered=True,
        id="54094",
        url="https://community.ovhcloud.com/t/s3-503/54094",
    ),
    thread(title="Can't create security groups on my instance", days=3, views=150, answered=False, id="2"),
    thread(title="How to mount a block storage volume?", days=4, answered=False, id="3"),
    thread(
        source="github",
        title="[BUG] ovh_cloud_project_kube fails on node pool update",
        days=1,
        answered=False,
        channel="ovh/terraform-provider-ovh",
        id="ovh/terraform-provider-ovh#1",
    ),
    thread(
        source="github",
        title="Release assets replaced, possible compromise",
        days=2,
        answered=True,
        replies=8,
        reactions=5,
        channel="ovh/ovhcloud-cli",
        id="ovh/ovhcloud-cli#300",
        url="https://github.com/ovh/ovhcloud-cli/issues/300",
    ),
    thread(
        source="roadmap",
        title="Managed Kubernetes: Karpenter autoscaler",
        days=5,
        reactions=38,
        labels=["Containers"],
        answered=True,
        replies=4,
        id="ovh/public-cloud-roadmap#10",
    ),
    thread(source="reddit", title="Public cloud billing jumped to 1500 € this month", days=1, id="t3_x"),
    thread(title="Old kubernetes question", days=20, answered=True, id="9"),
]

GUIDES = [
    Guide(
        "Creating and configuring a security group in Horizon",
        "Learn how to create security groups",
        "pages/public_cloud/compute/security_group",
        "public-cloud-compute-security-group",
        "compute",
    ),
    Guide(
        "Getting started with Object Storage",
        "S3 buckets and access keys",
        "pages/public_cloud/storage_and_backup/s3_start",
        "public-cloud-storage-s3-start",
        "storage_and_backup",
    ),
    Guide(
        "How to manage node pools on Managed Kubernetes",
        "Node pools, autoscaling",
        "pages/public_cloud/containers_orchestration/node_pools",
        "public-cloud-kube-node-pools",
        "containers_orchestration",
    ),
]


class FakeCollector(Collector):
    def __init__(self, threads=None, roadmap=None):
        super().__init__(client=None, github_token="")
        self._threads = list(THREADS if threads is None else threads)
        self._roadmap = roadmap if roadmap is not None else [t for t in self._threads if t.source == "roadmap"]

    async def threads(self, refresh=False):
        return list(self._threads)

    async def roadmap(self, refresh=False):
        return list(self._roadmap)

    async def first_response(self, t):
        if t.source == "forum" and t.answered:
            return t.created + timedelta(hours=2), True
        return None, False

    async def forum_topic(self, topic_id):
        return "S3 503", [
            Post("alice", NOW - timedelta(days=2), "GET returns 503"),
            Post("ovh_staff", NOW - timedelta(days=1), "We are looking into it", True),
        ]

    async def issue_timeline(self, repo, number):
        if number == 1:  # assigned, milestoned and linked to an open fix, but nobody told the reporter
            return [
                {"event": "labeled", "actor": {"login": "maintainer"}, "created_at": iso(NOW - timedelta(days=1))},
                {
                    "event": "assigned",
                    "actor": {"login": "maintainer"},
                    "assignee": {"login": "maintainer"},
                    "created_at": iso(NOW - timedelta(hours=5)),
                },
                {
                    "event": "milestoned",
                    "actor": {"login": "maintainer"},
                    "milestone": {"title": "v2.24"},
                    "created_at": iso(NOW - timedelta(hours=5)),
                },
                {
                    "event": "cross-referenced",
                    "actor": {"login": "maintainer"},
                    "created_at": iso(NOW),
                    "source": {
                        "issue": {
                            "number": 1552,
                            "title": "fix(kube): new kubeconfig",
                            "state": "open",
                            "html_url": "https://github.com/ovh/terraform-provider-ovh/pull/1552",
                            "pull_request": {"merged_at": None},
                        }
                    },
                },
            ]
        return [
            {
                "event": "commented",
                "user": {"login": "maintainer"},
                "author_association": "MEMBER",
                "created_at": iso(NOW),
            }
        ]

    async def issue_conversation(self, repo, number):
        t = next(x for x in self._threads if x.id == f"{repo}#{number}")
        return t, [Post(t.author, t.created, "body"), Post("maintainer", NOW, "fixed", True)]


@pytest.fixture
def collector():
    return FakeCollector()


@pytest.fixture
def docs():
    return DocsIndex(GUIDES)
