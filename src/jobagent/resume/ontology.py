"""Skill names, their other spellings, and which skills belong to the same family."""

from __future__ import annotations

import re

from jobagent.resume.evidence import SKILL_NAMES

EXTRA_TERMS = (
    "RAG", "LLM", "Kubernetes", "AWS", "Google Cloud", "Azure", "GraphQL", "Kafka", "CI/CD",
    "REST", "Terraform", "Celery", "RabbitMQ", "Elasticsearch", "Snowflake", "Spark", "Airflow",
    "MySQL", "SQL", "Node.js", "Git", "Linux", "Agile", "Microservices",
)  # fmt: skip
ALIASES: dict[str, str] = {
    "k8s": "Kubernetes",
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "nodejs": "Node.js",
    "node": "Node.js",
    "nextjs": "Next.js",
    "reactjs": "React",
    "react.js": "React",
    "vuejs": "Vue.js",
    "golang": "Go",
    "gcp": "Google Cloud",
    "google cloud platform": "Google Cloud",
    "llms": "LLM",
    "large language models": "LLM",
    "large language model": "LLM",
    "retrieval augmented generation": "RAG",
    "retrieval-augmented generation": "RAG",
    "cicd": "CI/CD",
    "js": "JavaScript",
    "ts": "TypeScript",
    "express": "Express.js",
    "mongo": "MongoDB",
    "hugging face": "Hugging Face Transformers",
    "gen ai": "Generative AI",
    "genai": "Generative AI",
}
_FAMILIES: dict[str, tuple[str, ...]] = {
    "agent frameworks": ("LangGraph", "LangChain", "CrewAI"),
    "vector databases": ("pgvector", "FAISS", "Chroma", "Pinecone"),
    "sql databases": ("PostgreSQL", "MySQL", "SQLAlchemy", "Prisma"),
    "python web": ("FastAPI", "Flask", "Django"),
    "node web": ("Express.js", "tRPC", "Next.js"),
    "frontend": ("React", "Vue.js", "Next.js", "Tailwind CSS"),
    "llm observability": ("Langfuse", "LangSmith"),
    "llm evaluation": ("RAGAS", "Langfuse"),
    "llm apis": ("Gemini API", "OpenAI API", "Claude API"),
    "llm serving": ("vLLM", "Hugging Face Transformers"),
    "fine-tuning": ("PEFT", "PyTorch", "bitsandbytes", "Hugging Face Transformers"),
    "cloud": ("AWS", "Google Cloud", "Azure"),
    "containers": ("Docker", "Kubernetes"),
    "testing": ("pytest", "Jest", "Playwright"),
    "queues": ("Kafka", "RabbitMQ", "Celery", "Inngest"),
}
_KNOWN = {name.lower(): name for name in (*SKILL_NAMES, *EXTRA_TERMS)}


def canonical(term: str) -> str:
    key = " ".join(term.lower().split())
    return ALIASES.get(key) or _KNOWN.get(key) or term.strip()


def _forms(name: str) -> list[str]:
    return [name, *(alias for alias, target in ALIASES.items() if target == name)]


def mention(name: str, text: str) -> str | None:
    """The spelling of this skill as written in the text, or None when it is not there."""
    for form in _forms(canonical(name)):
        found = re.search(rf"(?<![A-Za-z0-9]){re.escape(form)}(?![A-Za-z0-9])", text, re.I)
        if found:
            return found.group(0)
    return None


def related(a: str, b: str) -> bool:
    a, b = canonical(a), canonical(b)
    if a == b:
        return False
    return any(a in members and b in members for members in _FAMILIES.values())


def all_terms() -> tuple[str, ...]:
    return tuple(dict.fromkeys(_KNOWN.values()))
