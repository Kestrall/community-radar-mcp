"""Classification: product taxonomy (set by the active profile, see profiles.py) and signal detection (incident,
bug, billing…), in English and French."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .models import Thread

SIGNALS: dict[str, str] = {
    "incident": r"\b503\b|\b502\b|outage|incident|\bdown\b|\bpanne\b|\bhs\b|unavailable|indisponible|maintenance"
    r"|timeouts?|degraded|intermittent",
    "bug": r"\berror|\berreur|\bbug|fails?\b|failed|failing|impossible|can'?t|cannot|n'arrive pas|broken|crash"
    r"|ne fonctionne|doesn'?t work|not working|plante",
    "billing": r"factur|billing|invoice|charged|overcharg|refund|rembours|too expensive|trop cher|\bcost",
    "security": r"security|securite|hack|pirat|compromi|vulnerab|\bcve\b|leak|fuite|malware|phishing",
    "feature_request": r"feature|support for|would be (nice|great)|please add|\bajouter\b|possible d'avoir"
    r"|suggestion|request|roadmap|\ballow (us|me|users?|customers?|to)\b|\bpermettre",
    "question": r"\bhow\b|\bcomment\b|\bwhy\b|pourquoi|est-ce que|is it possible|\?",
}

NEGATIVE = ("incident", "bug", "billing", "security")
# Platform labels that say what kind of thread it is, not what it is about.
_TYPE_LABELS = re.compile(
    r"^(bug|feature|enhancement|question|help wanted|good first issue|duplicate|wontfix|invalid|solved|resolved"
    r"|resolu|answered|documentation|docs|triage|needs?[ -].*|status:.*|type:.*|priority:.*|demo|stale)$"
)


@dataclass
class Taxonomy:
    """What threads are about. Products: key -> (label, keywords regex on accent-free lowercase text, labels used
    by the platforms for it). `scope_in`/`scope_out` decide which imported threads belong to the community's scope;
    with `use_labels`, platform tags and labels become topics when no product matches."""

    products: dict[str, tuple[str, str, tuple[str, ...]]] = field(default_factory=dict)
    scope_in: str | None = None
    scope_out: str | None = None
    cross_cutting: frozenset[str] = frozenset()
    use_labels: bool = False

    def __post_init__(self) -> None:
        self.by_label = {lbl.lower(): key for key, (_, _, labels) in self.products.items() for lbl in labels}
        self.patterns = {key: re.compile(pattern) for key, (_, pattern, _) in self.products.items()}
        self.scope_in_re = re.compile(self.scope_in) if self.scope_in else None
        self.scope_out_re = re.compile(self.scope_out) if self.scope_out else None


_active = Taxonomy(use_labels=True)


def use_taxonomy(taxonomy: Taxonomy) -> None:
    """Switch the taxonomy every classification uses (the collector does it when the profile changes)."""
    global _active
    _active = taxonomy


def taxonomy() -> Taxonomy:
    return _active


_SIGNAL_RE = {key: re.compile(pattern) for key, pattern in SIGNALS.items()}
# Security groups and firewall rules are network configuration: they say nothing about a security problem or IAM.
_NETWORK_SECURITY = re.compile(r"security[ -]?groups?|groupes? de securite|firewall rules?|regles? de pare-?feu")
# Issue templates print the provider source ("registry.terraform.io/ovh/ovh"): not a Container Registry topic.
_NOISE = re.compile(r"registry\.(terraform\.io|opentofu\.org)\S*")
# On GitHub the issue type (title prefix or label) is more reliable than words found in the template.
_TYPE_BUG = re.compile(r"\[bug\]|^bug\b|type: bug|\bbug$")
_TYPE_FEATURE = re.compile(r"\[feature|^feat(ure)?\b|type: feature|enhancement|feature request")


def fold(text: str) -> str:
    """Lowercase and strip accents, so that French and English keywords match the same way."""
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def label(product: str) -> str:
    return _active.products[product][0] if product in _active.products else product


def resolve_product(name: str | None) -> str | None:
    """Accept a product key ("kubernetes"), its label ("Managed Kubernetes") or a fragment ("k8s", "s3")."""
    if not name:
        return None
    wanted = fold(name).strip()
    for key, (title, _, _) in _active.products.items():
        if wanted in (key, fold(title)) or wanted in fold(title):
            return key
    for key, pattern in _active.patterns.items():
        if pattern.search(wanted):
            return key
    if _active.use_labels:
        return wanted
    raise ValueError(f"Unknown product {name!r}. Known products: {', '.join(_active.products)}")


def in_scope(thread: Thread) -> bool:
    """Whether an imported thread (a subreddit or feed that covers more than the community) belongs to its scope.
    Without scope rules every thread does."""
    t = _active
    if not (t.scope_in_re or t.scope_out_re):
        return True
    text = fold(thread.text)
    if t.scope_in_re and t.scope_in_re.search(text):
        return True
    return bool(set(thread.products) - t.cross_cutting) and not (t.scope_out_re and t.scope_out_re.search(text))


def classify(thread: Thread) -> Thread:
    """Fill thread.products and thread.signals from its labels, title and excerpt."""
    text = _NOISE.sub(" ", fold(thread.text))
    t = _active
    products = [t.by_label[lbl.lower()] for lbl in thread.labels if lbl.lower() in t.by_label]
    plain = _NETWORK_SECURITY.sub(" network-rules ", text)
    products += [key for key, pattern in t.patterns.items() if pattern.search(plain if key == "iam_security" else text)]
    if not products and t.use_labels:
        products = [fold(lbl).strip() for lbl in thread.labels if not _TYPE_LABELS.match(fold(lbl).strip())]
    thread.products = list(dict.fromkeys(p for p in products if p))
    # Roadmap issues follow a template full of words like "error" or "cost": only their title tells the signal.
    signal_text = _NETWORK_SECURITY.sub(" network-rules ", fold(thread.title) if thread.source == "roadmap" else text)
    signals = [key for key, pattern in _SIGNAL_RE.items() if pattern.search(signal_text)]
    if thread.source == "roadmap" and "feature_request" not in signals:
        signals.append("feature_request")
    if thread.source == "github":
        kind = [fold(thread.title)] + [fold(lbl) for lbl in thread.labels]
        bug = any(_TYPE_BUG.search(k) for k in kind)
        if any(_TYPE_FEATURE.search(k) for k in kind) and not bug:
            signals = [s for s in signals if s != "bug"] + (
                ["feature_request"] if "feature_request" not in signals else []
            )
        elif bug and "bug" not in signals:
            signals.append("bug")
    thread.signals = signals
    return thread
