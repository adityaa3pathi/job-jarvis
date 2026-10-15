"""Job coach: what fits, what is missing and what to learn, then a grounded chat about it."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from pydantic import BaseModel, Field

from jobagent.llm import LLM, LLMCache, LLMError, ask, cache_key, data_block, model_id
from jobagent.resume.jd import JDProfile
from jobagent.resume.match import Coverage

MAX_JD_CHARS = 50_000
PROMPT_VERSION = "coach-2"
DIGEST_VERSION = "digest-1"
DIGEST_MIN_CHARS = 6_000  # shorter summaries (about 1.5k tokens) are sent as they are
MAX_HISTORY_CHARS = 6_000
MAX_REPLY_CHARS = 1_200

# One system text for report and chat, so both share the leading prompt (Gemini caching).
SYSTEM = (
    "You are a frank career coach for one job seeker. Use only the JOB, PROFILE, MATCH and "
    "CHAT blocks. Never claim the person has a skill the MATCH block marks as gap. Reply in "
    "short plain sentences, no jargon."
)
REPORT_STEP = (
    'Write the fit report. Return JSON exactly as {"fit": 1-5, "summary": "2 sentences", '
    '"good": [what already fits, at most 5], "learn": [{"topic", "why", "resource" '
    '(one free resource), "days" (rough number)} for each gap worth learning, at most 5], '
    '"risks": [likely interview weak spots, at most 3]}.'
)
CHAT_STEP = (
    "Answer the person's latest message in at most 120 words. If they state a new fact about "
    "their own experience, copy it into new_facts in their words; do not add facts they did "
    "not say. Never say you changed their profile: new facts wait for them to confirm on "
    'screen. Return JSON exactly as {"reply": "<your answer>", "new_facts": ["<fact>", ...]}.'
)
DIGEST_SYSTEM = (
    "Condense one person's career summary into a factual digest for a career coach, at most "
    "500 words. Keep roles, employers, dates, years, skills with where they were used, "
    'projects and numbers exactly as written; add nothing. Reply as {"digest": str}.'
)


class LearnItem(BaseModel):
    topic: str
    why: str
    resource: str = ""
    days: int = Field(default=0, ge=0, le=365)


class _Advice(BaseModel):
    fit: int = Field(ge=1, le=5)
    summary: str
    good: list[str] = Field(default_factory=list)
    learn: list[LearnItem] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class ChatTurn(BaseModel):
    reply: str
    new_facts: list[str] = Field(default_factory=list)


class _Digest(BaseModel):
    digest: str


@dataclass
class CoachReport:
    role_title: str
    fit: int
    summary: str
    good: list[str]
    proven: list[str]
    adjacent: list[str]
    missing_must: list[str]
    missing_nice: list[str]
    learn: list[LearnItem]
    risks: list[str]
    flagged_injection: bool = False
    degraded: bool = False

    def to_json(self) -> str:
        data = asdict(self)
        data["learn"] = [item.model_dump() for item in self.learn]
        return json.dumps(data)

    @classmethod
    def from_json(cls, raw: str) -> CoachReport:
        data = json.loads(raw)
        data["learn"] = [LearnItem.model_validate(item) for item in data["learn"]]
        return cls(**data)


@dataclass
class Message:
    role: str  # user | coach
    text: str


def profile_digest(summary: str, llm: LLM, cache: LLMCache | None = None) -> str:
    """A short form of a long career summary, made once per summary text and stored."""
    if len(summary) <= DIGEST_MIN_CHARS:
        return summary
    key = cache_key("profile_digest", DIGEST_VERSION, model_id(llm), summary)
    if cache is not None and (hit := cache.cache_get(key)):
        return hit
    try:
        digest = ask(
            llm, _Digest, system=DIGEST_SYSTEM, user=data_block("SUMMARY", summary),
            step="profile_digest",
        ).digest.strip()  # fmt: skip
    except LLMError:
        return summary  # costs more tokens, loses nothing
    if not digest:
        return summary
    if cache is not None:
        cache.cache_put(key, "profile_digest", digest)
    return digest


def shared_prefix(profile: JDProfile, digest: str) -> str:
    """JOB then PROFILE: identical for the report and every chat turn on one session."""
    job = json.dumps(
        {"title": profile.role_title, "level": profile.level, "years_min": profile.years_min,
         "work_mode": profile.work_mode, "responsibilities": profile.responsibilities[:12]}
    )  # fmt: skip
    return f"{data_block('JOB', job)}\n\n{data_block('PROFILE', digest)}\n\n"


def _split(coverage: Coverage) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"proven": [], "adjacent": [], "must": [], "nice": []}
    for item in coverage.items:
        if item.status == "gap":
            out["must" if item.need.importance == "must" else "nice"].append(item.need.name)
        else:
            out[item.status].append(item.need.name)
    return out


def build_report(
    profile: JDProfile, coverage: Coverage, digest: str, llm: LLM, cache: LLMCache | None = None
) -> CoachReport:
    """Skill lists come from the matcher; the model only adds the advice around them."""
    split = _split(coverage)
    match = json.dumps(
        {"proven": split["proven"], "adjacent": split["adjacent"],
         "gap_must": split["must"], "gap_nice": split["nice"], "score": coverage.score}
    )  # fmt: skip
    user = f"{shared_prefix(profile, digest)}{data_block('MATCH', match)}\n\n{REPORT_STEP}"
    key = cache_key("coach_report", PROMPT_VERSION, model_id(llm), SYSTEM, user)
    hit = cache.cache_get(key) if cache is not None else None
    if hit:
        advice = _Advice.model_validate_json(hit)
    else:
        advice = ask(llm, _Advice, system=SYSTEM, user=user, step="coach_report")
        if cache is not None:
            cache.cache_put(key, "coach_report", advice.model_dump_json())
    return CoachReport(
        role_title=profile.role_title,
        fit=advice.fit,
        summary=advice.summary,
        good=advice.good,
        proven=split["proven"],
        adjacent=split["adjacent"],
        missing_must=split["must"],
        missing_nice=split["nice"],
        learn=advice.learn,
        risks=advice.risks,
        flagged_injection=profile.injection_flagged,
        degraded=profile.degraded,
    )


def recent_history(history: list[Message], budget: int = MAX_HISTORY_CHARS) -> str:
    """The newest messages that fit in `budget` characters, oldest first."""
    lines: list[str] = []
    used = 0
    for m in reversed(history):
        line = f"{m.role}: {m.text}"
        if used + len(line) > budget:
            if not lines:
                lines.append(line[:budget])
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(reversed(lines))


def reply(
    report: CoachReport, digest: str, history: list[Message], message: str, llm: LLM,
    *, job: JDProfile,
) -> ChatTurn:  # fmt: skip
    match = json.dumps(
        {"fit": report.fit, "proven": report.proven, "adjacent": report.adjacent,
         "gap_must": report.missing_must, "gap_nice": report.missing_nice}
    )  # fmt: skip
    user = "\n\n".join(
        [shared_prefix(job, digest) + data_block("MATCH", match),
         data_block("CHAT", recent_history(history)), data_block("LATEST", message), CHAT_STEP]
    )  # fmt: skip
    turn = ask(llm, ChatTurn, system=SYSTEM, user=user, step="coach_chat")
    turn.reply = turn.reply.strip()[:MAX_REPLY_CHARS]
    return turn
