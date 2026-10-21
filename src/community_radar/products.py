"""Public Cloud product taxonomy and signal detection (incident, bug, billing…), in English and French."""

from __future__ import annotations

import re
import unicodedata

from .models import Thread

# key: (label, keywords, roadmap labels). Keywords are matched on accent-free lowercase text.
PRODUCTS: dict[str, tuple[str, str, tuple[str, ...]]] = {
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
        r"block storage|\bvolumes?\b|cinder|file storage|\bnfs\b",
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
        r"ai endpoints?|ai deploy|ai training|ai notebooks?|\bllm|inference|\bmodels?\b|ovhai|machine learning"
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

SIGNALS: dict[str, str] = {
    "incident": r"\b503\b|\b502\b|outage|incident|\bdown\b|\bpanne\b|\bhs\b|unavailable|indisponible|maintenance"
    r"|timeouts?|degraded|intermittent",
    "bug": r"\berror|\berreur|\bbug|fails?\b|failed|failing|impossible|can'?t|cannot|n'arrive pas|broken|crash"
    r"|ne fonctionne|doesn'?t work|not working|plante",
    "billing": r"factur|billing|invoice|charged|overcharg|refund|rembours|too expensive|trop cher|\bcost",
    "security": r"security|securite|hack|pirat|compromi|vulnerab|\bcve\b|leak|fuite|malware|phishing",
    "feature_request": r"feature|support for|would be (nice|great)|please add|\bajouter\b|possible d'avoir"
    r"|suggestion|request|roadmap|\ballow\b|\bpermettre",
    "question": r"\bhow\b|\bcomment\b|\bwhy\b|pourquoi|est-ce que|is it possible|\?",
}

NEGATIVE = ("incident", "bug", "billing", "security")
_LABEL_TO_PRODUCT = {label.lower(): key for key, (_, _, labels) in PRODUCTS.items() for label in labels}
_PRODUCT_RE = {key: re.compile(pattern) for key, (_, pattern, _) in PRODUCTS.items()}
_SIGNAL_RE = {key: re.compile(pattern) for key, pattern in SIGNALS.items()}


def fold(text: str) -> str:
    """Lowercase and strip accents, so that French and English keywords match the same way."""
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def label(product: str) -> str:
    return PRODUCTS[product][0] if product in PRODUCTS else product


def resolve_product(name: str | None) -> str | None:
    """Accept a product key ("kubernetes"), its label ("Managed Kubernetes") or a fragment ("k8s", "s3")."""
    if not name:
        return None
    wanted = fold(name).strip()
    for key, (title, _, _) in PRODUCTS.items():
        if wanted in (key, fold(title)) or wanted in fold(title):
            return key
    for key, pattern in _PRODUCT_RE.items():
        if pattern.search(wanted):
            return key
    raise ValueError(f"Unknown product {name!r}. Known products: {', '.join(PRODUCTS)}")


def classify(thread: Thread) -> Thread:
    """Fill thread.products and thread.signals from its labels, title and excerpt."""
    text = fold(thread.text)
    products = [_LABEL_TO_PRODUCT[lbl.lower()] for lbl in thread.labels if lbl.lower() in _LABEL_TO_PRODUCT]
    products += [key for key, pattern in _PRODUCT_RE.items() if pattern.search(text)]
    thread.products = list(dict.fromkeys(products))
    # Roadmap issues follow a template full of words like "error" or "cost": only their title tells the signal.
    signal_text = fold(thread.title) if thread.source == "roadmap" else text
    signals = [key for key, pattern in _SIGNAL_RE.items() if pattern.search(signal_text)]
    if thread.source == "roadmap" and "feature_request" not in signals:
        signals.append("feature_request")
    thread.signals = signals
    return thread
