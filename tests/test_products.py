import pytest

from community_radar.products import classify, fold, label, resolve_product
from tests.conftest import thread


def test_classifies_products_and_signals_in_french_and_english():
    t = thread(title="Base de données PostgreSQL indisponible après maintenance")
    assert "databases" in t.products
    assert "incident" in t.signals
    t = thread(title="Terraform provider fails when creating a kube node pool")
    assert {"iac_cli_api", "kubernetes"} <= set(t.products)
    assert "bug" in t.signals


def test_roadmap_labels_map_to_products_and_are_feature_requests():
    t = thread(source="roadmap", title="Something new", labels=["AI Endpoints"])
    assert t.products == ["ai"]
    assert "feature_request" in t.signals


def test_resolve_product_accepts_keys_labels_and_aliases():
    assert resolve_product("kubernetes") == "kubernetes"
    assert resolve_product("Managed Kubernetes") == "kubernetes"
    assert resolve_product("k8s") == "kubernetes"
    assert resolve_product("S3") == "object_storage"
    assert resolve_product(None) is None
    with pytest.raises(ValueError, match="Unknown product"):
        resolve_product("banana")


def test_helpers():
    assert fold("Données Été") == "donnees ete"
    assert label("ai") == "AI & Machine Learning"
    assert label("other") == "other"
    assert classify(thread(title="Nothing relevant here")).products == []


def test_issue_template_noise_does_not_classify():
    tf = "Terraform v1.14 + provider registry.terraform.io/ovh/ovh v2.13.0"
    t = thread(source="github", title="[BUG] ovh_ip_firewall_rule inconsistent result", excerpt=tf)
    assert "registry" not in t.products and "network" in t.products
    assert "registry" in thread(title="Harbor private registry quota").products
    vps = thread(source="github", title="ovh_vps: expose post_install_script", excerpt="model `vps.rebuild.post`")
    assert "ai" not in vps.products
    assert "ai" in thread(title="Which LLM models are available on AI Endpoints?").products


def test_github_issue_type_beats_template_words():
    feature = thread(
        source="github",
        title="[FEATURE] Attach a volume to an instance",
        excerpt="Describe the bug: the error is that we cannot",
        labels=["Type: Feature"],
    )
    assert "bug" not in feature.signals and "feature_request" in feature.signals
    bug = thread(source="github", title="Instances list is empty", labels=["Type: Bug"])
    assert "bug" in bug.signals
