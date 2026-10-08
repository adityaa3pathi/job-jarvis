"""Fit score 1 to 5: fixed rules set a ceiling, a model judge scores within it (R4, KTD13)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from jobagent.facts import FactSheet, Track
from jobagent.llm import LLM, LLMError, ask, data_block
from jobagent.models import DropReason, Posting
from jobagent.requirements import extract_requirements
from jobagent.rules import check_hard_rules, years_asked
from jobagent.sanitize import clean_posting_text

AI_TERMS = (
    "langgraph", "langchain", "llm", "llms", "genai", "generative ai", "agentic", "ai agent",
    "ai agents", "rag", "mcp", "vector", "embeddings", "prompt", "fine-tuning", "ai engineer",
    "ml engineer", "machine learning",
)  # fmt: skip
FS_TERMS = (
    "next.js", "nextjs", "react", "node.js", "nodejs", "full stack", "full-stack", "fullstack",
    "typescript", "prisma", "express", "tailwind", "frontend", "front-end", "mern",
)  # fmt: skip
JUDGE_SYSTEM = (
    "Score how well one job posting fits the candidate, 1 (poor) to 5 (strong), from stack "
    'overlap and seniority. Reply {"score": int, "reasons": [str], "track": "ai"|"full_stack"}. '
    "Use only the candidate facts given."
)


class Judgement(BaseModel):
    score: int = Field(ge=1, le=5)
    reasons: list[str] = Field(min_length=1)
    track: Literal["ai", "full_stack"]


@dataclass
class RankResult:
    status: Literal["ranked", "dropped", "unscored"]
    score: int | None = None
    track: Track | None = None
    reasons: list[str] = field(default_factory=list)
    drop_reason: DropReason | None = None
    ceiling: int | None = None
    bar: int = 4
    model_calls: int = 0

    @property
    def passes_bar(self) -> bool:
        return self.status == "ranked" and self.score is not None and self.score >= self.bar

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


def _count(text: str, terms: tuple[str, ...]) -> int:
    return sum(1 for t in terms if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", text))


def rule_track(title: str, body: str) -> Track | None:
    """Track from keywords. Title hits count triple so a full-stack title wins."""
    t, b = title.lower(), body.lower()
    ai = _count(b, AI_TERMS) + 3 * _count(t, AI_TERMS)
    fs = _count(b, FS_TERMS) + 3 * _count(t, FS_TERMS)
    if ai == fs:
        return None
    return "ai" if ai > fs else "full_stack"


def rule_ceiling(
    sheet: FactSheet, posting: Posting, clean: str, today: date, rounding: str
) -> tuple[int, list[str]]:
    req = extract_requirements(sheet, clean)
    total = len(req.skills) + len(req.unknown)
    reasons: list[str] = []
    if total == 0:
        base = 3
        reasons.append("no stack words found, capped at 3")
    else:
        have = sum(1 for s in req.skills if sheet.skills[s].confirmed)
        share = have / total
        base = (
            5
            if share >= 0.75
            else 4
            if share >= 0.5
            else 3
            if share >= 0.3
            else 2
            if share >= 0.15
            else 1
        )
        reasons.append(f"stack overlap {have} of {total} named tools")
    owner = sheet.experience_years(today, rounding)
    asked = years_asked(posting)
    if asked:
        low, high = asked
        if low >= owner + 3:
            base -= 2
            reasons.append(f"asks {low}+ years, you have about {owner}")
        elif low >= owner + 2:
            base -= 1
            reasons.append(f"asks {low}+ years, you have about {owner}")
        elif high is not None and high < owner:
            base -= 1
            reasons.append(f"asks up to {high} years, below your level")
        else:
            reasons.append("experience range fits")
    return max(1, min(5, base)), reasons


def _clean_reason(text: str) -> str:
    return re.sub(r"[\x00-\x1f]+", " ", text).strip()[:200]


def judge(llm: LLM, user: str) -> tuple[Judgement | None, int]:
    """One retry on a bad reply. Returns (judgement or None, number of calls made)."""
    for attempt in (1, 2):
        try:
            return ask(llm, Judgement, system=JUDGE_SYSTEM, user=user), attempt
        except LLMError:
            continue
    return None, 2


def rank_posting(
    posting: Posting,
    sheet: FactSheet,
    llm: LLM | None,
    *,
    bar: int = 4,
    max_years_asked: int = 5,
    today: date | None = None,
    rounding: str = "down",
) -> RankResult:
    today = today or date.today()
    dropped = check_hard_rules(posting, max_years_asked)
    if dropped:
        return RankResult("dropped", drop_reason=dropped, reasons=[dropped], bar=bar)
    clean, _flagged = clean_posting_text(
        f"{posting.title}\n{posting.description}\n{' '.join(posting.stack_words)}"
    )
    ceiling, reasons = rule_ceiling(sheet, posting, clean, today, rounding)
    track = rule_track(posting.title, clean)
    result = RankResult("unscored", track=track, reasons=reasons, ceiling=ceiling, bar=bar)
    if llm is None:
        return result
    skills = ", ".join(s.canonical for s in sheet.skills.values() if s.confirmed)
    user = (
        data_block(
            "candidate",
            f"Years of experience: {sheet.experience_years(today, rounding)}\n"
            f"Confirmed skills: {skills}",
        )  # fmt: skip
        + "\n"
        + data_block(
            "posting", f"Title: {posting.title}\nLocation: {posting.location}\n{clean[:6000]}"
        )
    )
    verdict, calls = judge(llm, user)
    result.model_calls = calls
    if verdict is None:
        result.reasons.append("judge output invalid, left unscored")
        return result
    result.track = track or verdict.track
    # The judge may lower the rule ceiling but never raise it.
    result.score = min(verdict.score, ceiling)
    result.status = "ranked"
    result.reasons += [_clean_reason(r) for r in verdict.reasons[:3]]
    return result
