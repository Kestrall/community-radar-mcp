"""Profiles: what kind of community the radar watches. The core is neutral; a profile adds a product taxonomy, the
scope of imported channels, a documentation index, a title and preset sources.

The profile is detected from the sources (a forum on ovhcloud.com, ovh/* repositories, r/ovh… select "OVHcloud Public
Cloud") or chosen on the Sources page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .products import Taxonomy

if TYPE_CHECKING:
    from .config import SourceConfig

# key: (label, keywords, roadmap labels). Keywords are matched on accent-free lowercase text.
OVHCLOUD_PRODUCTS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "compute": (
        "Compute instances",
        r"\binstances?\b|\bvm\b|flavou?r|\bgpu\b|l40s|h100|a100|l4\b|snapshot|cloud-init|\bnova\b|\bimage\b"
        r"|\bb3-|\bc3-|\br3-|\bd2-|\bb2-|metal instance|rescue",
        ("Compute", "GPU", "Image Lifecycle"),
    ),
    "kubernetes": (
        "Managed Kubernetes",
        r"kubernetes|\bk8s\b|\bmks\b|kubectl|node ?pool|\bhelm\b|ingress|kubeconfig|rancher",
        ("Containers",),
    ),
    "object_storage": (
        "Object Storage",
        r"object storage|\bs3\b|bucket|\bswift\b|cold archive|high perf(ormance)? storage",
        ("Object Storage", "Storage"),
    ),
    "block_storage": (
        "Block & File Storage",
        r"block storage|\bvolumes?\b|cinder|file storage|\bnfs\b|\bbackups?\b|sauvegarde",
        ("Block Storage", "File Storage", "backup"),
    ),
    "network": (
        "Network",
        r"vrack|private network|reseau prive|load ?balancer|octavia|floating ip|\bgateway\b|public ip|ip publique"
        r"|security groups?|firewall|pare-?feu|\bdns\b|\bvpn\b|\bsubnet",
        ("Network", "Geo"),
    ),
    "databases": (
        "Managed Databases",
        r"database|base de donnees|postgres|mysql|mongo|redis|valkey|kafka|opensearch|clickhouse|cassandra|\bm3db",
        (
            "Managed Databases",
            "PostgreSQL",
            "MySQL",
            "MongoDB",
            "Redis",
            "Valkey",
            "Kafka",
            "OpenSearch",
            "ClickHouse",
            "Cassandra",
            "M3db",
        ),
    ),
    "ai": (
        "AI & Machine Learning",
        r"ai endpoints?|ai deploy|ai training|ai notebooks?|\bllm|inference|\b(ai|llm|language|vision) models?\b"
        r"|ovhai|machine learning"
        r"|\bml\b|qwen|llama|mistral|embedding",
        ("AI Deploy", "AI Endpoints", "AI Notebooks", "AI Training", "ML Serving", "Machine Learning Services"),
    ),
    "data_platform": (
        "Data & Analytics",
        r"data platform|data processing|spark|flink|lakehouse|data catalog|analytics|grafana|dashboards?",
        (
            "Analytics Manager",
            "Data Catalog",
            "Data Integration",
            "Data Platform",
            "Data Processing",
            "Data Processing Engine",
            "Flink",
            "Lakehouse Manager",
            "Spark",
            "Grafana",
            "Observability",
        ),
    ),
    "iac_cli_api": (
        "API, CLI & IaC",
        r"terraform|pulumi|ovhcloud-cli|\bcli\b|\bapi\b|\bsdk\b|horizon|openstack (cli|client)|ansible"
        r"|provider|\btoken\b",
        ("API/CLI/UI", "APP/API", "CLI"),
    ),
    "registry": ("Container Registry", r"registry|harbor", ()),
    "billing": (
        "Billing & quotas",
        r"billing|factur|invoice|pricing|\bprices?\b|\bprix\b|\bcosts?\b|\bcout|voucher|credits?\b|quota|charged"
        r"|\beur\b|€",
        ("Billing",),
    ),
    "iam_security": (
        "IAM & security",
        r"\biam\b|identity|\b2fa\b|mfa|permissions?|access key|security|securite|compliance|kms|secret",
        ("Identity Access Manager", "Security & Compliance"),
    ),
}


# Generic cloud taxonomy: the same families, without any provider's own names.
CLOUD_PRODUCTS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "compute": (
        "Compute instances",
        r"\binstances?\b|\bvms?\b|virtual machines?|flavou?r|\bgpu\b|h100|a100|l40s|snapshot|cloud-init|\bimage\b"
        r"|autoscal\w* group|rescue mode",
        ("Compute", "GPU"),
    ),
    "kubernetes": (
        "Kubernetes",
        r"kubernetes|\bk8s\b|kubectl|node ?pool|\bhelm\b|ingress|kubeconfig|\beks\b|\baks\b|\bgke\b|rancher",
        ("Kubernetes", "Containers"),
    ),
    "object_storage": (
        "Object Storage",
        r"object storage|\bs3\b|bucket|blob storage|\bgcs\b|cold archive",
        ("Storage",),
    ),
    "block_storage": (
        "Block & File Storage",
        r"block storage|\bvolumes?\b|\bebs\b|file storage|\bnfs\b|\bbackups?\b|sauvegarde",
        ("Backup",),
    ),
    "network": (
        "Network",
        r"\bvpc\b|private network|reseau prive|load ?balancer|floating ip|\bgateway\b|public ip|ip publique"
        r"|security groups?|firewall|pare-?feu|\bdns\b|\bvpn\b|\bsubnet|\bcdn\b",
        ("Network",),
    ),
    "databases": (
        "Managed Databases",
        r"database|base de donnees|postgres|mysql|mongo|redis|valkey|kafka|opensearch|elasticsearch|clickhouse",
        ("Databases",),
    ),
    "ai": (
        "AI & Machine Learning",
        r"\bllm|inference|\b(ai|llm|language|vision) models?\b|machine learning|\bml\b|embedding|fine-?tun",
        ("AI", "Machine Learning"),
    ),
    "iac_cli_api": (
        "API, CLI & IaC",
        r"terraform|opentofu|pulumi|\bcli\b|\bapi\b|\bsdk\b|ansible|provider|\btoken\b",
        ("API", "CLI", "SDK"),
    ),
    "billing": (
        "Billing & quotas",
        r"billing|factur|invoice|pricing|\bprices?\b|\bprix\b|\bcosts?\b|\bcout|credits?\b|quota|charged|€|\$\d",
        ("Billing",),
    ),
    "iam_security": (
        "IAM & security",
        r"\biam\b|identity|\b2fa\b|mfa|permissions?|access key|security|securite|compliance|kms|secret",
        ("Security", "IAM"),
    ),
}

# Imported channels (r/ovh…) speak about every OVHcloud product: only Public Cloud threads are kept.
OVHCLOUD_SCOPE_IN = r"public cloud|cloud public|\bpci\b|openstack|horizon"
OVHCLOUD_SCOPE_OUT = (
    r"\bvps\b|dedicated server|serveurs? dedies?|bare ?metal server|kimsufi|so ?you ?start|\beco\b range"
    r"|\bdomains?\b|\bdomaines?\b|whois|\bdkim\b|registrar|web ?hosting|hebergement web|\bhosting\b|cluster\d{3}"
    r"|e-?mail pro|exchange|private cloud|vmware|nutanix|\bsms\b|telephon"
)
# Billing and IAM cut across every product: on their own they do not place a thread in the scope.
CROSS_CUTTING = frozenset({"billing", "iam_security"})


@dataclass(frozen=True)
class Profile:
    key: str
    name: str  # what the community is about, e.g. "OVHcloud Public Cloud"
    vendor: str  # who runs it, for the "not affiliated with" note; empty for a neutral profile
    taxonomy: Taxonomy
    docs: bool = False  # ships with the bundled documentation index
    staff_roles: str = ""  # extra Discord role names that mark staff, e.g. "ovhcloud"
    team_role: str = "a member of the community team"  # voice used when drafting replies
    detect: str | None = None  # regex on source identifiers that selects this profile
    preset: dict[str, Any] = field(default_factory=dict)  # sources loaded by "Load preset sources"

    @property
    def title(self) -> str:
        return f"{self.name} Community Radar" if self.name else "Community Radar"


GENERIC = Profile("generic", "", "", Taxonomy(use_labels=True))
CLOUD = Profile("cloud", "Cloud", "", Taxonomy(CLOUD_PRODUCTS, cross_cutting=CROSS_CUTTING, use_labels=True))
OVHCLOUD = Profile(
    "ovhcloud",
    "OVHcloud Public Cloud",
    "OVHcloud",
    Taxonomy(OVHCLOUD_PRODUCTS, OVHCLOUD_SCOPE_IN, OVHCLOUD_SCOPE_OUT, CROSS_CUTTING),
    docs=True,
    staff_roles="ovhcloud",
    team_role="an OVHcloud Tech Community Manager",
    detect=r"ovhcloud\.com|^ovh/|github\.com/ovh/|^r/ovh$|(^|[./])ovh\.(com|net|ie|co\.uk)",
    preset={
        "forums": [
            {"url": "https://community.ovhcloud.com", "category": "public-cloud/11", "label": "OVHcloud Community"}
        ],
        "roadmap": "ovh/public-cloud-roadmap",
        "github": [{"repo": "ovh/terraform-provider-ovh"}, {"repo": "ovh/ovhcloud-cli"}],
        "reddit": [{"subreddit": "ovh"}],
    },
)
PROFILES = {p.key: p for p in (GENERIC, CLOUD, OVHCLOUD)}


def identifiers(config: SourceConfig) -> list[str]:
    """Everything that names a source: forum URLs, repositories, subreddits, feed URLs."""
    return (
        [f["url"] for f in config.forums]
        + ([config.roadmap] if config.roadmap else [])
        + [g["repo"] for g in config.github]
        + [f"r/{r['subreddit']}" for r in config.reddit]
        + [f["url"] for f in config.feeds]
    )


def detect(config: SourceConfig) -> Profile:
    """The profile whose sources these are (generic when none matches)."""
    ids = [i.lower() for i in identifiers(config)]
    for profile in PROFILES.values():
        if profile.detect and any(re.search(profile.detect, i) for i in ids):
            return profile
    return GENERIC


def resolve(config: SourceConfig) -> Profile:
    return detect(config) if config.profile == "auto" else PROFILES[config.profile]
