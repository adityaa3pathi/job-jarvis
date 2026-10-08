"""Free-text answers: retrieve facts, draft, check, then wait for the owner (KTD18)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from jobagent.facts import FactSheet
from jobagent.graph_state import draft_hash
from jobagent.index import FactIndex
from jobagent.llm import LLM, LLMError, ask, data_block
from jobagent.truth import TruthChecker

Status = Literal["draft", "no_match", "owner_only", "blocked"]
MAX_ANSWER_CHARS = 600
MAX_FACTS = 6
MIN_SCORE = 0.2
_SENSITIVE = re.compile(
    r"salary|ctc|compensation|pay\b|notice|join|visa|sponsor|authori[sz]|work permit|"
    r"gender|race|ethnic|disab|veteran|religio|caste|marital|pronoun|date of birth|"
    r"current employer|references?",
    re.IGNORECASE,
)
_BROAD = re.compile(
    r"everything|all (?:of )?your|full (?:history|list)|each (?:project|job)|every (?:project|job)|"
    r"contact|phone|email|address|home town",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")

SYSTEM = (
    "You answer one job-application question for the candidate in first person, using only "
    "the facts given. Never add a tool, number, employer or claim that is not in the facts. "
    'Reply {"answer": str, "fact_ids": [ids of the facts you used]}.'
)


class Draft(BaseModel):
    answer: str = Field(max_length=MAX_ANSWER_CHARS)
    fact_ids: list[str] = Field(max_length=MAX_FACTS)


class SensitiveJudge(BaseModel):
    sensitive: bool


@dataclass
class AnswerDraft:
    status: Status
    answer: str = ""
    fact_ids: list[str] = field(default_factory=list)
    hash: str | None = None
    reason: str = ""


def scrub(text: str) -> str:
    return _PHONE.sub("[removed]", _EMAIL.sub("[removed]", text))


def is_sensitive(question: str, llm: LLM | None = None) -> bool:
    """Keywords first. If a model is set it can only add a sensitive verdict, never clear one."""
    if _SENSITIVE.search(question):
        return True
    if llm is None:
        return False
    try:
        result = ask(
            llm,
            SensitiveJudge,
            system="Is this form question about pay, notice period, visa or work rights, "
            'or personal demographics? Reply {"sensitive": bool}.',
            user=data_block("question", scrub(question)),
        )
    except LLMError:
        return True
    return result.sensitive


def draft_answer(
    question: str,
    sheet: FactSheet,
    index: FactIndex,
    llm: LLM,
    checker: TruthChecker,
    judge: LLM | None = None,
) -> AnswerDraft:
    if is_sensitive(question, judge):
        return AnswerDraft("owner_only", reason="sensitive question")
    if _BROAD.search(question):
        return AnswerDraft("owner_only", reason="asks for broad or contact data")
    hits = index.search(scrub(question), k=MAX_FACTS, min_score=MIN_SCORE)
    if not hits:
        return AnswerDraft("no_match", reason="no fact matched the question")
    facts = "\n".join(f"{h.fact_id}: {scrub(h.text)}" for h in hits)
    user = data_block("facts", facts) + "\n" + data_block("question", scrub(question))
    try:
        draft = ask(llm, Draft, system=SYSTEM, user=user)
    except LLMError as exc:
        return AnswerDraft("blocked", reason=str(exc))
    allowed = {h.fact_id for h in hits}
    cited = [f for f in draft.fact_ids if f in allowed]
    if not cited or not draft.answer.strip():
        return AnswerDraft("blocked", reason="draft cites no retrieved fact")
    report = checker.check_unit(draft.answer, cited)
    if report.blocked:
        return AnswerDraft("blocked", reason=", ".join(v.code for v in report.violations))
    return AnswerDraft("draft", draft.answer, cited, draft_hash(draft.answer))
