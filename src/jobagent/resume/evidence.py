"""Evidence cards: small facts about the owner, each with where it came from."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

DEP_SKILLS: dict[str, str] = {
    "fastapi": "FastAPI",
    "flask": "Flask",
    "django": "Django",
    "express": "Express.js",
    "next": "Next.js",
    "react": "React",
    "vue": "Vue.js",
    "prisma": "Prisma",
    "@trpc": "tRPC",
    "inngest": "Inngest",
    "redis": "Redis",
    "ioredis": "Redis",
    "mongoose": "MongoDB",
    "pymongo": "MongoDB",
    "sqlalchemy": "SQLAlchemy",
    "asyncpg": "PostgreSQL",
    "psycopg2": "PostgreSQL",
    "psycopg2-binary": "PostgreSQL",
    "pg": "PostgreSQL",
    "pgvector": "pgvector",
    "langchain": "LangChain",
    "langgraph": "LangGraph",
    "crewai": "CrewAI",
    "langfuse": "Langfuse",
    "langsmith": "LangSmith",
    "ragas": "RAGAS",
    "vllm": "vLLM",
    "peft": "PEFT",
    "transformers": "Hugging Face Transformers",
    "torch": "PyTorch",
    "bitsandbytes": "bitsandbytes",
    "faiss-cpu": "FAISS",
    "chromadb": "Chroma",
    "pinecone-client": "Pinecone",
    "google-genai": "Gemini API",
    "openai": "OpenAI API",
    "anthropic": "Claude API",
    "@modelcontextprotocol": "MCP",
    "mcp": "MCP",
    "pydantic": "Pydantic",
    "pytest": "pytest",
    "jest": "Jest",
    "playwright": "Playwright",
    "tailwindcss": "Tailwind CSS",
    "typescript": "TypeScript",
    "docker": "Docker",
    "ai": "Vercel AI SDK",
}
LANGUAGES = ("Python", "TypeScript", "JavaScript", "Go", "Java", "Rust", "C++", "C#", "Kotlin")
SKILL_NAMES = tuple(sorted(set(DEP_SKILLS.values()) | set(LANGUAGES), key=len, reverse=True))
_NAME = re.compile(r"[A-Za-z0-9_.@/\-]+")


@dataclass(frozen=True)
class EvidenceCard:
    id: str
    kind: str  # skill | project | claim
    text: str
    sources: tuple[str, ...]
    confirmed: bool


def make_card(kind: str, text: str, source: str, confirmed: bool) -> EvidenceCard:
    digest = hashlib.sha1(f"{kind}|{text.lower()}".encode()).hexdigest()[:8]
    return EvidenceCard(f"{kind}-{digest}", kind, text, (source,), confirmed)


def _skill_for(dep: str) -> str | None:
    name = dep.strip().lower().replace("_", "-")
    return DEP_SKILLS.get(name) or DEP_SKILLS.get(name.split("/")[0])


def _spec_name(spec: object) -> str | None:
    match = _NAME.match(spec.strip()) if isinstance(spec, str) else None
    return match.group(0) if match else None


def _dependencies(path: str, text: str) -> list[str]:
    base = PurePosixPath(path).name
    try:
        if base == "package.json":
            data = json.loads(text)
            return [*data.get("dependencies", {}), *data.get("devDependencies", {})]
        if base == "pyproject.toml":
            data = tomllib.loads(text)
            specs = list(data.get("project", {}).get("dependencies", []))
            for group in (
                *data.get("project", {}).get("optional-dependencies", {}).values(),
                *data.get("dependency-groups", {}).values(),
            ):
                specs += group
            return [n for n in map(_spec_name, specs) if n]
    except (ValueError, AttributeError, TypeError):
        return []
    lines = [ln.strip() for ln in text.splitlines()]
    return [n for ln in lines if ln and ln[0] not in "#-" if (n := _spec_name(ln))]


def cards_from_manifests(manifests: dict[str, str], source_prefix: str) -> list[EvidenceCard]:
    cards: list[EvidenceCard] = []
    for path, text in manifests.items():
        for dep in _dependencies(path, text):
            if skill := _skill_for(dep):
                cards.append(make_card("skill", skill, f"{source_prefix}:{path}", False))
    return merge_cards(cards)


def cards_from_summary(text: str) -> list[EvidenceCard]:
    """Lines the owner wrote count as confirmed; skills are found by exact word match."""
    cards = [
        make_card("claim", line.strip(), "owner", True)
        for line in text.splitlines()
        if line.strip()
    ]
    for skill in SKILL_NAMES:
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(skill)}(?![A-Za-z0-9])", text, re.IGNORECASE):
            cards.append(make_card("skill", skill, "owner", True))
    return merge_cards(cards)


def merge_cards(cards: list[EvidenceCard]) -> list[EvidenceCard]:
    merged: dict[str, EvidenceCard] = {}
    for card in cards:
        seen = merged.get(card.id)
        if seen is None:
            merged[card.id] = card
            continue
        sources = tuple(dict.fromkeys(seen.sources + card.sources))
        merged[card.id] = replace(seen, sources=sources, confirmed=seen.confirmed or card.confirmed)
    return list(merged.values())
