"""Interview prep for one job: likely questions with short answers built only from the
owner's own profile. Each answer about the owner quotes the profile line it rests on, and
a rules check flags any quote the profile does not hold."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from jobagent.db import bump_version
from jobagent.llm import LLM, ask, data_block

PROMPT_VERSION = "prep-v1"
MAX_JD = 8_000
MAX_PROFILE = 14_000
QUOTE_MATCH = 0.7  # share of a quote's words that one profile line must hold

SYSTEM = (
    "You prepare one job seeker for an interview for the JOB. Use only the JOB and PROFILE "
    "blocks. Never invent experience, numbers, employers or tools that PROFILE does not "
    "state. Short plain sentences, no jargon, in the first person as the job seeker."
)
STEP = (
    "Return JSON exactly as {"
    '"pitch": [3 to 5 bullets for "tell me about yourself", tuned to this JOB], '
    '"questions": [10 to 16 items {"kind": "about_you" | "technical" | "behavioral" | '
    '"company", "question": "...", "answer": [1 to 5 short bullets], "based_on": "a line '
    "copied word for word from PROFILE that the answer rests on, or empty for technical "
    'and company questions"}], '
    '"gaps": [up to 5 {"topic": "a JOB must-have PROFILE lacks", "say": "an honest answer"}], '
    '"ask_them": [3 to 5 questions to ask the interviewer]}. '
    "Pick the questions this JOB's interviewers are most likely to ask. Behavioral answers "
    "use situation, action, result from PROFILE only."
)

Kind = Literal["about_you", "technical", "behavioral", "company"]


class PrepQuestion(BaseModel):
    kind: Kind
    question: str = Field(max_length=400)
    answer: list[str] = Field(max_length=6)
    based_on: str = Field(default="", max_length=600)
    grounded: bool = True  # set by check_quotes, never by the model


class PrepGap(BaseModel):
    topic: str = Field(max_length=200)
    say: str = Field(max_length=600)


class Prep(BaseModel):
    pitch: list[str] = Field(max_length=6)
    questions: list[PrepQuestion] = Field(max_length=20)
    gaps: list[PrepGap] = Field(default_factory=list, max_length=6)
    ask_them: list[str] = Field(default_factory=list, max_length=6)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def quote_found(quote: str, profile: str) -> bool:
    """True when one profile line holds most of the quote's words (3 words at least)."""
    want = set(_words(quote))
    if len(want) < 3:
        return False
    lines = (set(_words(line)) for line in profile.splitlines())
    return any(len(want & line) / len(want) >= QUOTE_MATCH for line in lines)


def check_quotes(prep: Prep, profile: str) -> Prep:
    """Mark answers about the owner whose quote is not in the profile; the page warns."""
    for q in prep.questions:
        if q.kind in ("about_you", "behavioral"):
            q.grounded = quote_found(q.based_on, profile)
    return prep


def make_prep(*, title: str, company: str, jd: str, profile: str, llm: LLM) -> Prep:
    job = f"{title} at {company}\n\n{jd[:MAX_JD]}"
    user = f"{data_block('JOB', job)}\n\n{data_block('PROFILE', profile[:MAX_PROFILE])}\n\n{STEP}"
    prep = ask(llm, Prep, system=SYSTEM, user=user, step="interview_prep", version=PROMPT_VERSION)
    return check_quotes(prep, profile)


class PrepStore:
    """Preps in the jobs database (table: db.PREP_SCHEMA); the newest per job is shown."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def add(self, job_id: int, prep: Prep) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO preps(job_id, body, created_at) VALUES (?,?,?)",
                (job_id, prep.model_dump_json(), datetime.now(UTC).isoformat()),
            )
            bump_version(self.conn)
        return int(cur.lastrowid or 0)

    def latest(self, job_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT id, body, created_at FROM preps WHERE job_id=? ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "created_at": row["created_at"], **json.loads(row["body"])}
