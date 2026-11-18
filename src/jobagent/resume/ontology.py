"""Skill names, their other spellings, and which skills belong to the same family."""

from __future__ import annotations

import re

from jobagent.resume.evidence import SKILL_NAMES

EXTRA_TERMS = (
    "RAG", "LLM", "Kubernetes", "AWS", "Google Cloud", "Azure", "GraphQL", "Kafka", "CI/CD",
    "REST", "Terraform", "Celery", "RabbitMQ", "Elasticsearch", "Snowflake", "Spark", "Airflow",
    "MySQL", "SQL", "NoSQL", "Node.js", "Git", "Linux", "Agile", "Microservices",
    "Machine Learning", "Deep Learning", "NLP", "Computer Vision", "OCR", "Generative AI",
    "TensorFlow", "Keras", "scikit-learn", "Pandas", "NumPy", "LlamaIndex", "Fine-tuning",
    "LoRA", "QLoRA", "Prompt Engineering", "MLOps", "MLflow", "Weaviate", "Milvus", "Qdrant",
    "Spring Boot", "Hadoop", "Bash",
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
    "amazon web services": "AWS",
    "llms": "LLM",
    "large language models": "LLM",
    "large language model": "LLM",
    "retrieval augmented generation": "RAG",
    "retrieval-augmented generation": "RAG",
    "cicd": "CI/CD",
    "ci": "CI/CD",
    "restful": "REST",
    "js": "JavaScript",
    "ts": "TypeScript",
    "express": "Express.js",
    "mongo": "MongoDB",
    "hugging face": "Hugging Face Transformers",
    "huggingface": "Hugging Face Transformers",
    "gen ai": "Generative AI",
    "genai": "Generative AI",
    "ml": "Machine Learning",
    "dl": "Deep Learning",
    "natural language processing": "NLP",
    "fine tuning": "Fine-tuning",
    "finetuning": "Fine-tuning",
    "sklearn": "scikit-learn",
}
# Spellings that are also everyday words ("the planner node", "go live", "the rest of"):
# they name the skill only when written in this exact case.
_CASED = {"go": "Go", "node": "Node", "express": "Express", "ci": "CI", "rest": "REST",
          "js": "JS", "ts": "TS", "ml": "ML", "dl": "DL"}  # fmt: skip
_FAMILIES: dict[str, tuple[str, ...]] = {
    "agent frameworks": ("LangGraph", "LangChain", "CrewAI", "LlamaIndex"),
    "vector databases": ("pgvector", "FAISS", "Chroma", "Pinecone", "Weaviate", "Milvus", "Qdrant"),
    "sql databases": ("PostgreSQL", "MySQL", "SQLAlchemy", "Prisma"),
    "python web": ("FastAPI", "Flask", "Django"),
    "node web": ("Express.js", "tRPC", "Next.js"),
    "frontend": ("React", "Vue.js", "Next.js", "Tailwind CSS"),
    "llm observability": ("Langfuse", "LangSmith"),
    "llm evaluation": ("RAGAS", "Langfuse"),
    "llm apis": ("Gemini API", "OpenAI API", "Claude API"),
    "llm serving": ("vLLM", "Hugging Face Transformers"),
    "fine-tuning": (
        "PEFT",
        "PyTorch",
        "bitsandbytes",
        "Hugging Face Transformers",
        "LoRA",
        "QLoRA",
    ),
    "deep learning": ("PyTorch", "TensorFlow", "Keras"),
    "cloud": ("AWS", "Google Cloud", "Azure"),
    "containers": ("Docker", "Kubernetes"),
    "testing": ("pytest", "Jest", "Playwright"),
    "queues": ("Kafka", "RabbitMQ", "Celery", "Inngest"),
}
_LLM_WORK = ("LangChain", "LangGraph", "LlamaIndex", "CrewAI", "vLLM", "RAG", "Gemini API",
             "OpenAI API", "Claude API", "QLoRA", "LoRA", "Fine-tuning", "Prompt Engineering",
             "Hugging Face Transformers", "MCP", "Langfuse", "LangSmith", "RAGAS")  # fmt: skip
# A general skill is shown by any specific one: pgvector work is vector-database work.
IMPLIED_BY: dict[str, tuple[str, ...]] = {
    "LLM": _LLM_WORK,
    "Generative AI": _LLM_WORK,
    "Vector Databases": ("pgvector", "FAISS", "Chroma", "Pinecone", "Weaviate", "Milvus", "Qdrant"),
    "SQL": ("PostgreSQL", "MySQL", "SQLAlchemy", "Prisma"),
    "NoSQL": ("MongoDB", "Redis", "Elasticsearch"),
    "Deep Learning": ("PyTorch", "TensorFlow", "Keras"),
    "Fine-tuning": ("QLoRA", "LoRA", "PEFT"),
    "LoRA": ("QLoRA",),
    "REST": ("FastAPI", "Flask", "Django", "Express.js"),
}
_KNOWN = {name.lower(): name for name in (*SKILL_NAMES, *EXTRA_TERMS)}
_PAREN = re.compile(r"(.+?)\s*\(([^()]+)\)")
_LIST_MARK = re.compile(r"[&/,+]|\band\b", re.I)


def _lookup(term: str) -> str | None:
    key = " ".join(term.lower().split())
    return ALIASES.get(key) or _KNOWN.get(key)


def canonical(term: str) -> str:
    """The one name for a skill. "Large Language Models (LLMs)" and "RAG (hybrid retrieval)"
    are LLM and RAG; a combined need like "Embeddings & Vector DBs (pgvector)" stays whole."""
    if found := _lookup(term):
        return found
    if (m := _PAREN.fullmatch(term.strip())) and not _LIST_MARK.search(m.group(1)):
        for half in (m.group(1), m.group(2)):
            if found := _lookup(half):
                return found
    return term.strip()


def _forms(name: str) -> list[str]:
    return [name, *(alias for alias, target in ALIASES.items() if target == name)]


def _patterns(name: str) -> list[re.Pattern[str]]:
    out = []
    for form in _forms(canonical(name)):
        cased = _CASED.get(form.lower())
        body = rf"(?<![A-Za-z0-9]){re.escape(cased or form)}(?![A-Za-z0-9])"
        out.append(re.compile(body, 0 if cased else re.I))
    return out


def mention(name: str, text: str) -> str | None:
    """The spelling of this skill as written in the text, or None when it is not there."""
    for pattern in _patterns(name):
        if found := pattern.search(text):
            return found.group(0)
    return None


def positions(name: str, text: str) -> list[int]:
    """Where the text names this skill, in any of its spellings."""
    return sorted({m.start() for p in _patterns(name) for m in p.finditer(text)})


def stem(word: str) -> str:
    """A rough word root, so "tuning" and "fine-tune" or "APIs" and "API" meet."""
    return re.sub(r"(ations?|ions?|ing|ers?|ed|es|s)$", "", word.lower()).removesuffix("e")


def says(name: str, text: str) -> bool:
    """Named as written, or every word of it in some form: "Monitoring" in "uptime monitor"."""
    if mention(name, text):
        return True
    want = {stem(w) for w in re.findall(r"[A-Za-z]{4,}", name)}
    have = {stem(w) for w in re.findall(r"[A-Za-z]{3,}", text)}
    return bool(want) and want <= have


def implied_by(name: str) -> tuple[str, ...]:
    """Specific skills that each show this general one; empty for a specific skill."""
    key = canonical(name).lower()
    return next((v for k, v in IMPLIED_BY.items() if k.lower() == key), ())


def related(a: str, b: str) -> bool:
    a, b = canonical(a), canonical(b)
    if a == b:
        return False
    return any(a in members and b in members for members in _FAMILIES.values())


def known(term: str) -> bool:
    """True for a skill this file knows by name or alias."""
    return _lookup(canonical(term)) is not None


def all_terms() -> tuple[str, ...]:
    return tuple(dict.fromkeys(_KNOWN.values()))
