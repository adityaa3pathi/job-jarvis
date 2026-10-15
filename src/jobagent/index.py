"""Local search over confirmed facts, used to ground free-text answers (KTD18)."""

from __future__ import annotations

import hashlib
import logging
import math
import re
from array import array
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from jobagent.facts import FactSheet

log = logging.getLogger(__name__)
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
    sim_range = (0.0, 0.6)  # cosine of unrelated and closely related text, for ranking

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
    sim_range = (0.55, 0.9)  # bge-small gives even unrelated English text about 0.5

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


# --- posting and profile vectors for ranking (U14) ---


class VectorStore(Protocol):
    def vectors(self, keys: Sequence[str]) -> dict[str, bytes]: ...

    def save_vectors(self, model: str, items: Mapping[str, bytes]) -> None: ...


def load_embedder() -> Embedder | None:
    """The fastembed model, or None (logged) when it is not installed or cannot load:
    ranking then scores on rules and preferences only."""
    try:
        return FastEmbedEmbedder()
    except Exception as exc:  # ImportError, a failed download, a broken model file
        log.warning("embedding model unavailable, ranking without it: %s", type(exc).__name__)
        return None


def vector_key(embedder: Embedder, text: str) -> str:
    return hashlib.sha256(f"{embedder.name}\x00{text}".encode()).hexdigest()


def pack(vec: Sequence[float]) -> bytes:
    return array("f", vec).tobytes()


def unpack(blob: bytes) -> array[float]:
    vec = array("f")
    vec.frombytes(blob)
    return vec  # 4 bytes a value: a run holds thousands of these


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Both vectors are unit length already."""
    return sum(x * y for x, y in zip(a, b, strict=True))


def embed_cached(
    embedder: Embedder, texts: Sequence[str], store: VectorStore, batch: int = 64
) -> list[array[float]]:
    """Vectors for `texts`; each distinct text is embedded once, ever, per model."""
    keys = [vector_key(embedder, t) for t in texts]
    stored = store.vectors(keys)
    missing = list(dict.fromkeys(k for k in keys if k not in stored))
    text_of = dict(zip(keys, texts, strict=True))
    for start in range(0, len(missing), batch):
        part = missing[start : start + batch]
        fresh = {k: pack(v) for k, v in zip(part, embedder.embed([text_of[k] for k in part]),
                                            strict=True)}  # fmt: skip
        store.save_vectors(embedder.name, fresh)
        stored |= fresh
    return [unpack(stored[k]) for k in keys]
