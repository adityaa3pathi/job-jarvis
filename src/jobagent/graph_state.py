"""Shapes shared by the fillers and the run graph. State holds IDs and hashes, not text (KTD16)."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, Field

Kind = Literal["draft", "owner_only", "unmapped", "sensitive"]
PAUSE_DAYS = 7


class PausedQuestion(BaseModel):
    """One open question on a form. The draft text lives in the database, not here."""

    field_label: str
    question: str
    kind: Kind
    draft_hash: str | None = None
    fact_ids: list[str] = Field(default_factory=list)


class PausedResult(BaseModel):
    """What a filler returns when it cannot finish. The graph turns it into one interrupt."""

    job_id: str
    questions: list[PausedQuestion]


class Approval(BaseModel):
    """The owner's yes, tied to one exact draft."""

    job_id: str
    field_label: str
    draft_hash: str


def draft_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def approval_matches(approval: Approval, text: str) -> bool:
    """True only when the text to type is exactly the text the owner approved."""
    return approval.draft_hash == draft_hash(text)
