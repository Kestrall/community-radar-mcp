"""Common shape for every community signal, whatever the channel it comes from."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Thread:
    """A question, issue or post raised by the community."""

    source: str  # forum | roadmap | github | reddit
    id: str
    title: str
    url: str
    created: datetime
    last_activity: datetime
    excerpt: str = ""
    author: str = ""
    replies: int = 0
    views: int = 0
    reactions: int = 0  # likes on the forum, 👍 votes on GitHub
    answered: bool | None = None  # someone other than the author replied (None: unknown)
    solved: bool = False  # accepted answer on the forum, closed issue on GitHub
    labels: list[str] = field(default_factory=list)
    channel: str = ""  # e.g. "ovh/terraform-provider-ovh", "r/ovh"
    staff_author: bool = False  # opened by a maintainer (internal work item, not a community question)
    products: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)

    @property
    def engagement(self) -> float:
        """Rough attention score: how many people this thread reached or mobilised."""
        return self.views / 100 + self.replies + 2 * self.reactions

    @property
    def text(self) -> str:
        return f"{self.title} {self.excerpt} {' '.join(self.labels)}"


@dataclass
class Guide:
    """An official OVHcloud guide from the ovh/docs repository."""

    title: str
    excerpt: str
    path: str
    slug: str
    section: str
    updated: str = ""

    def url(self, lang: str = "en") -> str:
        prefix = "fr" if lang.lower().startswith("fr") else "en-gb"
        return f"https://help.ovhcloud.com/csm/{prefix}-{self.slug}"
