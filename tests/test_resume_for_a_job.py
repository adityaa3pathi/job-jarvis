"""End to end: an AI Engineer posting and the sample resume, the way a recruiter would read it."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from jobagent.llm import StubLLM
from jobagent.resume.evidence import cards_from_summary
from jobagent.resume.latex import parse
from jobagent.resume.pipeline import run_tailor

HERE = Path(__file__).parent / "fixtures" / "resume"
JD = (HERE / "ai_engineer_jd.txt").read_text()
EXTRACT = json.loads((HERE / "ai_engineer_extract.json").read_text())
SAMPLE = (HERE / "sample.tex").read_text()
SUMMARY = (
    "AI engineer at DocNow: LangGraph agents, hybrid RAG with pgvector, Redis memory.\n"
    "Python, FastAPI, PostgreSQL and Docker in production; PyTorch, Git and Linux daily.\n"
    "Fine-tuned Mistral-7B with QLoRA using Hugging Face; vLLM on Kubernetes on Google Cloud.\n"
    "Prompt engineering, machine learning and NLP; MongoDB and AWS (EC2, S3)."
)


def respond(system: str, user: str) -> Any:
    if "job posting" in system:
        return EXTRACT
    return {"items": []} if "Rewrite each" in system else {"text": ""}


def build() -> Any:
    return run_tailor(JD, SAMPLE, cards_from_summary(SUMMARY), StubLLM(respond), add_gaps=False)


def skills_text(latex: str) -> str:
    return parse(latex).section("Technical Skills").emit()


def test_every_job_skill_the_owner_has_is_listed_in_skills() -> None:
    out = build()
    shown = skills_text(out.tailored.latex)
    owned = ("Python", "PyTorch", "LangGraph", "LangChain", "Kubernetes", "FastAPI", "PostgreSQL",
             "Docker", "Redis", "Machine Learning", "NLP", "Prompt Engineering", "REST APIs", "LLM",
             "Generative AI", "AWS", "Google Cloud", "Git", "Linux")  # fmt: skip
    for skill in owned:
        assert re.search(rf"(?<![A-Za-z]){re.escape(skill)}(?![A-Za-z])", shown), skill


def test_nothing_the_owner_lacks_is_put_on_the_resume() -> None:
    out = build()
    shown = skills_text(out.tailored.latex)
    for skill in ("TensorFlow", "LlamaIndex", "Agile", "Kafka", "Spark", "MLflow", "Weaviate"):
        assert skill not in shown, skill
    assert out.tailored.added_gap_skills == []


def test_the_owners_own_skills_fill_the_room_left_and_cuts_are_reported() -> None:
    out = build()
    shown = skills_text(out.tailored.latex)
    assert "TypeScript" in shown and shown.index("Python") < shown.index("TypeScript")
    assert out.tailored.dropped_skills  # this posting fills most of the 36 places
    for name in out.tailored.dropped_skills:
        assert name not in shown and f"- {name}" in out.report


def test_report_lists_every_job_skill_and_the_real_gaps() -> None:
    report = build().report
    assert "## Skills the job names: 50" in report
    lines = report.splitlines()
    gaps = next(ln for ln in lines if ln.startswith("- Must-have, not in your profile"))
    near = next(ln for ln in lines if ln.startswith("- Must-have, related skill only"))
    assert "Agile" in gaps and "Claude API" in gaps
    assert "TensorFlow (you have PyTorch)" in near and "LlamaIndex" in near
    named = re.sub(r"\(you have [^)]*\)", "", gaps + near)
    for skill in ("Python", "LangGraph", "REST APIs", "LLM", "Vector Databases", "SQL", "NoSQL"):
        assert not re.search(rf"[:,] {re.escape(skill)}(,|$)", named), skill
