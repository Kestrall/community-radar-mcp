"""Index of the official Public Cloud guides (github.com/ovh/docs) and a small keyword search over it."""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections import Counter
from importlib import resources
from pathlib import Path

import httpx

from .models import Guide
from .products import fold

DOCS_REPO = "ovh/docs"
DOCS_BRANCH = "develop"
SECTION = "pages/public_cloud/"
STOPWORDS = set(
    "a an and are as at be by can de des du en est et for from how i in is it la le les my of on or ou par"
    " pas pour que qui sur the to un une use using with without what why when your vos votre mon ma mes"
    " ne n je j il ce cette ces dans avec sans comment pourquoi quand guide ovhcloud ovh cloud public".split()
)


def tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9][a-z0-9+.-]*[a-z0-9]|[a-z0-9]", fold(text))
    return [stem(w) for w in words if w not in STOPWORDS]


def stem(word: str) -> str:
    """Crude stemming so that autoscaler, autoscaling and autoscaled match each other."""
    for suffix in ("ings", "ing", "ers", "er", "ed", "es", "s"):
        if len(word) > len(suffix) + 4 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def front_matter(text: str) -> dict[str, str]:
    match = re.match(r"---\s*\n(.*?)\n---", text, re.S)
    fields = {}
    for line in (match.group(1) if match else text).splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() and not key.startswith(" "):
            fields[key.strip()] = value.strip().strip("'\"")
    return fields


class DocsIndex:
    """Keyword index (BM25) over guide titles, excerpts and folder names."""

    def __init__(self, guides: list[Guide]) -> None:
        self.guides = guides
        self._docs = [
            tokens(f"{g.title} {g.title} {g.excerpt} {g.section.replace('_', ' ')} {g.slug.replace('-', ' ')}")
            for g in guides
        ]
        self._tf = [Counter(d) for d in self._docs]
        df = Counter(w for d in self._docs for w in set(d))
        n = max(len(guides), 1)
        self._idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}
        self._avg = sum(map(len, self._docs)) / n if guides else 1

    def search(self, query: str, limit: int = 5, section: str | None = None) -> list[tuple[Guide, float]]:
        terms = [t for t in tokens(query) if t in self._idf]
        scored = []
        for guide, tf, doc in zip(self.guides, self._tf, self._docs, strict=True):
            if section and section not in guide.section:
                continue
            score = 0.0
            for t in terms:
                f = tf.get(t, 0)
                score += self._idf[t] * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * len(doc) / self._avg))
            if score > 0:
                scored.append((guide, round(score, 2)))
        scored.sort(key=lambda x: -x[1])
        return scored[:limit]

    def to_json(self) -> str:
        return json.dumps([g.__dict__ for g in self.guides], ensure_ascii=False, indent=0)

    @classmethod
    def from_json(cls, text: str) -> DocsIndex:
        return cls([Guide(**g) for g in json.loads(text)])

    @classmethod
    def bundled(cls) -> DocsIndex:
        """Index shipped with the package (rebuild it with `community-radar build-docs`)."""
        return cls.from_json(resources.files("community_radar").joinpath("docs_index.json").read_text())

    @classmethod
    def load(cls, path: str | Path | None) -> DocsIndex:
        if path and Path(path).exists():
            return cls.from_json(Path(path).read_text())
        return cls.bundled()


async def build_index(client: httpx.AsyncClient, concurrency: int = 16) -> DocsIndex:
    """Read the English guide headers and meta.yaml of every Public Cloud guide (about 1,000 small requests)."""
    tree = (await client.get(f"https://api.github.com/repos/{DOCS_REPO}/git/trees/{DOCS_BRANCH}?recursive=1")).json()
    folders = sorted(
        t["path"].removesuffix("/guide.en-gb.md")
        for t in tree["tree"]
        if t["path"].startswith(SECTION) and t["path"].endswith("/guide.en-gb.md")
    )
    raw = f"https://raw.githubusercontent.com/{DOCS_REPO}/{DOCS_BRANCH}/"
    semaphore = asyncio.Semaphore(concurrency)

    async def head(path: str) -> str:
        async with semaphore:
            response = await client.get(raw + path, headers={"Range": "bytes=0-1500"})
            return response.text if response.status_code in (200, 206) else ""

    async def guide(folder: str) -> Guide | None:
        header, meta = await asyncio.gather(head(f"{folder}/guide.en-gb.md"), head(f"{folder}/meta.yaml"))
        fields, meta_fields = front_matter(header), front_matter(meta)
        if not fields.get("title") or not meta_fields.get("full_slug"):
            return None
        return Guide(
            title=fields["title"],
            excerpt=fields.get("excerpt", ""),
            path=folder,
            slug=meta_fields["full_slug"],
            section=folder.removeprefix(SECTION).split("/")[0],
            updated=fields.get("updated", ""),
        )

    guides = [g for g in await asyncio.gather(*map(guide, folders)) if g]
    return DocsIndex(guides)
