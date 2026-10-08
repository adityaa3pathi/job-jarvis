"""Local search over confirmed facts, used to ground free-text answers (KTD18)."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Protocol

from jobagent.facts import FactSheet

# Pinned so a first-run download is the same file every time (KTD18).
FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"
_WORD = re.compile(r"[a-z0-9][a-z0-9.+#]*")
_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "we",
        "what",
        "when",
        "where",
        "which",
        "who",
        "with",
        "you",
        "your",
        "about",
        "tell",
        "us",
        "describe",
        "me",
        "my",
        "i",
        "have",
        "has",
        "had",
        "do",
        "did",
        "experience",
        "built",
    ]
)


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Offline bag-of-words vectors. Needs no download, so tests and demos run anywhere."""

    name = "hashing-1024"

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for word in _WORD.findall(text.lower()):
            if word in _STOP:
                continue
            digest = hashlib.md5(word.encode(), usedforsecurity=False).digest()
            vec[int.from_bytes(digest[:4], "big") % self.dim] += 1.0
        return _unit(vec)


class FastEmbedEmbedder:
    """ONNX model through fastembed. Optional extra, fetched by `job-agent setup`."""

    name = f"fastembed:{FASTEMBED_MODEL}"

    def __init__(self) -> None:
        from fastembed import TextEmbedding  # type: ignore[import-not-found,unused-ignore]

        self._model = TextEmbedding(model_name=FASTEMBED_MODEL)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [_unit([float(x) for x in v]) for v in self._model.embed(texts)]


def _unit(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


@dataclass(frozen=True)
class Hit:
    fact_id: str
    text: str
    score: float


def indexable_facts(sheet: FactSheet) -> list[tuple[str, str]]:
    """Confirmed role, project, bullet and skill facts only. Contact answers never enter."""
    out: list[tuple[str, str]] = []
    for role in sheet.roles.values():
        if role.confirmed:
            out.append((role.id, f"{role.titles[0]} at {role.employer} {role.descriptor}".strip()))
    for project in sheet.projects.values():
        if project.confirmed:
            out.append((project.id, f"{project.names[0]}. Tech: {', '.join(project.tech)}"))
    for bullet in sheet.bullets.values():
        if bullet.confirmed:
            out.append((bullet.id, bullet.text))
    for skill in sheet.skills.values():
        if skill.confirmed:
            out.append((skill.id, skill.canonical))
    return out


class FactIndex:
    def __init__(self, sheet: FactSheet, embedder: Embedder) -> None:
        self.embedder = embedder
        self.fingerprint = f"{sheet.hash}:{embedder.name}"
        self.items = indexable_facts(sheet)
        self._vectors = embedder.embed([text for _, text in self.items])

    def is_current(self, sheet: FactSheet) -> bool:
        return self.fingerprint == f"{sheet.hash}:{self.embedder.name}"

    def search(self, query: str, k: int = 4, min_score: float = 0.0) -> list[Hit]:
        if not self.items:
            return []
        q = self.embedder.embed([query])[0]
        scored = [
            Hit(fid, text, sum(a * b for a, b in zip(q, vec, strict=True)))
            for (fid, text), vec in zip(self.items, self._vectors, strict=True)
        ]
        scored.sort(key=lambda h: h.score, reverse=True)
        return [h for h in scored[:k] if h.score > min_score]


def ensure_index(sheet: FactSheet, embedder: Embedder, current: FactIndex | None) -> FactIndex:
    """Reuse the index unless facts.yaml or the embedder changed."""
    return (
        current if current is not None and current.is_current(sheet) else FactIndex(sheet, embedder)
    )
