from community_radar.docs import DocsIndex, front_matter, stem, tokens


def test_front_matter_and_tokens():
    fields = front_matter("---\ntitle: 'CLI - Installation'\nexcerpt: Learn how\nupdated: 2023-03-31\n---\n## Body")
    assert fields == {"title": "CLI - Installation", "excerpt": "Learn how", "updated": "2023-03-31"}
    assert front_matter("id: 123\nfull_slug: public-cloud-x") == {"id": "123", "full_slug": "public-cloud-x"}
    assert tokens("How to create the Security Groups?") == ["create", "security", "group"]
    assert stem("autoscaling") == stem("autoscaler") == "autoscal"


def test_search_ranks_the_matching_guide_first(docs):
    guide, score = docs.search("cannot create security group")[0]
    assert guide.title.startswith("Creating and configuring a security group")
    assert score > 0
    assert docs.search("kubernetes autoscaler", section="containers")[0][0].slug.endswith("node-pools")
    assert docs.search("zzzz") == []


def test_urls_and_serialisation(docs):
    guide = docs.guides[0]
    assert guide.url() == "https://help.ovhcloud.com/csm/en-gb-public-cloud-compute-security-group"
    assert guide.url("fr").startswith("https://help.ovhcloud.com/csm/fr-")
    again = DocsIndex.from_json(docs.to_json())
    assert [g.title for g in again.guides] == [g.title for g in docs.guides]


def test_bundled_index_covers_public_cloud(tmp_path):
    bundled = DocsIndex.bundled()
    assert len(bundled.guides) > 300
    assert DocsIndex.load(tmp_path / "missing.json").guides == bundled.guides
    path = tmp_path / "index.json"
    path.write_text(DocsIndex(bundled.guides[:2]).to_json())
    assert len(DocsIndex.load(path).guides) == 2
