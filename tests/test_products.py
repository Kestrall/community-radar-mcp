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
