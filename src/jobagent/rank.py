"""Fit score 1 to 5: fixed rules set a ceiling, a model judge scores within it (R4, KTD13).
Tiered ranking (U14): a cheap 0-100 score on every found job, the judge on the top N."""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, Field

from jobagent.db import Database, judge_key, text_hash, today_ist
from jobagent.dedupe import normalize_company
from jobagent.facts import FactSheet, Track
from jobagent.index import Embedder, cosine, embed_cached
from jobagent.lexicon import KNOWN_TECH
from jobagent.llm import (
    LLM,
    LLMCache,
    LLMError,
    Usage,
    ask,
    cache_key,
    data_block,
    model_id,
    usage_sink,
)
from jobagent.models import DropReason, Posting
from jobagent.requirements import extract_requirements
from jobagent.rules import check_hard_rules, years_asked
from jobagent.sanitize import clean_posting_text

if TYPE_CHECKING:
    from jobagent.config import LLMSettings, Settings

log = logging.getLogger(__name__)

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


def share_base(share: float) -> int:
    """Ceiling 1-5 from the share of named tools the owner has."""
    return 5 if share >= 0.75 else 4 if share >= 0.5 else 3 if share >= 0.3 else (
        2 if share >= 0.15 else 1
    )  # fmt: skip


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
        base = share_base(have / total)
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


def judge(llm: LLM, user: str, step: str = "") -> tuple[Judgement | None, int]:
    """One retry on a bad reply. Returns (judgement or None, number of calls made)."""
    for attempt in (1, 2):
        try:
            return ask(llm, Judgement, system=JUDGE_SYSTEM, user=user, step=step), attempt
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


# --- tiered ranking of every found job (U14, R20) ---

STACK_TERMS = KNOWN_TECH | frozenset(AI_TERMS) | frozenset(FS_TERMS) | frozenset((
    "python", "java", "javascript", "go", "c++", "c#", ".net", "sql", "postgres", "postgresql",
    "mongodb", "redis", "docker", "kubernetes", "aws", "fastapi", "node", "pgvector", "langgraph",
    "langchain", "rag", "mcp", "spring", "kotlin", "oracle", "sap", "abap", "cobol", "html",
    "css", "redux", "jest", "linux", "git", "supabase", "firebase", "vercel", "nestjs", "llm",
))  # fmt: skip
_PHRASES = tuple(t for t in STACK_TERMS if " " in t)
_TOKEN = re.compile(r"[a-z0-9.+#][a-z0-9.+#-]*")
FEATURES = ("role", "source", "company", "work_mode")
JUDGE_STEP = "rank_judge"
JUDGE_VERSION = "1"
PROFILE_CHARS = 3000  # profile text the embedder and the judge see
EMBED_CHUNK = 500


def stack_terms(text: str) -> set[str]:
    lowered = text.lower()
    tokens = {t.strip(".-") for t in _TOKEN.findall(lowered)}
    found = (tokens | {f".{t}" for t in tokens if t == "net"}) & STACK_TERMS
    return found | {p for p in _PHRASES if p in lowered}


def profile_ceiling(
    profile: str | frozenset[str], posting: str, min_years: int | None, owner_years: int | None
) -> tuple[int, list[str]]:
    """The rule ceiling from the owner's profile text (or its `stack_terms`, worked out once
    per run), so it works without facts.yaml."""
    if isinstance(profile, str) and not profile.strip():
        return 3, ["no profile yet, capped at 3"]
    mine = frozenset(stack_terms(profile)) if isinstance(profile, str) else profile
    named = stack_terms(posting)
    if not named:
        base, reasons = 3, ["no stack words found, capped at 3"]
    else:
        have = len(named & mine)
        base, reasons = share_base(have / len(named)), [f"stack {have} of {len(named)} tools"]
    if owner_years is not None and min_years is not None and min_years >= owner_years + 2:
        base -= 2 if min_years >= owner_years + 3 else 1
        reasons.append(f"asks {min_years}+ years, you have about {owner_years}")
    return max(1, min(5, base)), reasons


def _company(job: Mapping[str, Any]) -> str:
    return normalize_company(str(job.get("company") or ""))


class Preferences:
    """Learned from owner marks: smoothed yes/no log-odds per role, source, company and
    work mode, each against the overall yes rate (naive Bayes). No labels gives 0.5."""

    def __init__(self, counts: dict[tuple[str, str], list[int]], yes: int, no: int) -> None:
        self.counts, self.yes, self.no = counts, yes, no

    @staticmethod
    def _value(job: Mapping[str, Any], feature: str) -> str:
        return _company(job) if feature == "company" else str(job.get(feature) or "")

    @classmethod
    def fit(cls, labels: Sequence[Mapping[str, Any]]) -> Preferences:
        counts: dict[tuple[str, str], list[int]] = {}
        for row in labels:
            for f in FEATURES:
                if value := cls._value(row, f):
                    counts.setdefault((f, value), [0, 0])[0 if row["label"] else 1] += 1
        yes = sum(1 for r in labels if r["label"])
        return cls(counts, yes, len(labels) - yes)

    def predict(self, job: Mapping[str, Any]) -> tuple[float, str]:
        """Chance the owner wants this job, and the strongest reason, if any."""
        if not self.yes + self.no:
            return 0.5, ""
        prior = math.log((self.yes + 1) / (self.no + 1))
        logit, top, why = 0.0, 0.0, ""
        for f in FEATURES:
            value = self._value(job, f)
            yes, no = self.counts.get((f, value), (0, 0))
            if not value or not yes + no:
                continue
            delta = math.log((yes + 1) / (no + 1)) - prior
            logit += delta
            if abs(delta) > abs(top):
                top = delta
                why = f"{'liked' if delta > 0 else 'skipped'} {f.replace('_', ' ')} {value}"
        return 1 / (1 + math.exp(-logit)), why if abs(top) >= 0.3 else ""


def cheap_score(ceiling: int, sim: float | None, pref: float) -> int:
    """0-100 from the rule ceiling, profile similarity (0-1, None without a model) and the
    preference term; never above the ceiling's share of 100."""
    rule = ceiling / 5
    raw = 0.75 * rule + 0.25 * pref if sim is None else 0.45 * rule + 0.35 * sim + 0.2 * pref
    return round(100 * min(raw, rule))


def _scaled(embedder: Embedder, sim: float) -> float:
    """Cosine to 0-1: each model has its own range for related text."""
    low, high = getattr(embedder, "sim_range", (0.55, 0.9))
    return max(0.0, min(1.0, (sim - low) / (high - low)))


def with_judge(score: int, reason: str, cap: int | None, why: str) -> tuple[int, str]:
    """The judge may only lower the score, and says why when it does."""
    if cap is None or cap >= score:
        return score, reason
    return cap, f"{reason}; AI judge: {why}"[:300]


@dataclass
class RankSummary:
    scored: int = 0
    changed: int = 0
    embedded: bool = False
    left_to_embed: int = 0  # found jobs still waiting for a vector (filled on later runs)


def score_found(
    db: Database,
    embedder: Embedder | None,
    *,
    owner_years: int | None = None,
    max_new_embeddings: int | None = None,
) -> RankSummary:
    """Tier 2: a cheap score for every found job; writes only rows whose score moved."""
    rows, profile = db.rank_rows(), db.profile_text()
    prefs = Preferences.fit(db.labels())
    sims: list[float | None] = [None] * len(rows)
    left = 0
    if embedder is not None and profile and rows:
        heads = [f"{r['title']}\n{r['head']}" for r in rows]
        budget = max_new_embeddings
        try:
            [me] = embed_cached(embedder, [profile[:PROFILE_CHARS]], db)
            if me is None:
                raise ValueError("profile vector missing")
            sims = []
            for start in range(0, len(heads), EMBED_CHUNK):  # bounds vectors held at once
                done: list[int] = []
                part = embed_cached(
                    embedder,
                    heads[start : start + EMBED_CHUNK],
                    db,
                    max_new=budget,
                    embedded=done,
                )
                if budget is not None:
                    budget = max(0, budget - done[0])
                sims += [cosine(me, v) if v is not None else None for v in part]
            left = sum(1 for s in sims if s is None)
        except Exception as exc:  # a model failure must not stop the run
            log.warning("embedding failed, ranking on rules only: %s", type(exc).__name__)
            sims = [None] * len(rows)
    mine = frozenset(stack_terms(profile)) if profile else ""
    changed: list[tuple[int | None, str, int]] = []
    for row, sim in zip(rows, sims, strict=True):
        text = f"{row['title']}\n{row['head']}\n{' '.join(json.loads(row['stack_words']))}"
        ceiling, why = profile_ceiling(mine, text, row["min_years"], owner_years)
        pref, liked = prefs.predict(row)
        scaled = None
        if sim is not None and embedder is not None:
            scaled = _scaled(embedder, sim)
            why.append(f"profile match {round(100 * scaled)}%")
        # A verdict on other posting text or another profile no longer counts.
        current = row["judge_key"] == judge_key(row["desc_hash"], profile)
        score, reason = with_judge(
            cheap_score(ceiling, scaled, pref), "; ".join([*why, *([liked] if liked else [])]),
            row["judge_score"] if current else None, row["judge_reason"] or "",
        )  # fmt: skip
        if (score, reason) != (row["score"], row["rank_reason"]):
            changed.append((score, reason, row["id"]))
    db.save_ranks(changed)
    return RankSummary(len(rows), len(changed), any(s is not None for s in sims), left)


class JudgeStore(LLMCache, Protocol):
    def add_usage(self, usage: Usage) -> None: ...

    def usage_today(self, day: str | None = None) -> dict[str, int]: ...


def over_budget(used: Mapping[str, int], budget: LLMSettings) -> bool:
    """The U5 daily budget; a limit of 0 means none."""
    limits = (("tokens", budget.daily_token_budget), ("calls", budget.daily_call_budget))
    return any(limit and used[kind] >= limit for kind, limit in limits)


@dataclass
class JudgeSummary:
    judged: int = 0
    calls: int = 0
    cache_hits: int = 0
    stopped_by_budget: bool = False
    failed: bool = False


def judge_top(
    db: Database, st: JudgeStore, llm: LLM, *, top_n: int, budget: LLMSettings
) -> JudgeSummary:
    """Tier 3: the AI judge on the best found jobs not judged on their current text and
    profile, at most `top_n` per IST day, inside the daily AI budget. Verdicts are cached
    by posting and profile hash."""
    out, profile = JudgeSummary(), db.profile_text()
    day = today_ist().isoformat()
    left = top_n - db.judged_on(day)
    if not profile or left <= 0:
        return out
    me = data_block("candidate", profile[:PROFILE_CHARS])
    for job in db.judge_candidates(left, profile):
        clean, _ = clean_posting_text(f"{job['title']}\n{job['head']}")
        key = cache_key(JUDGE_STEP, JUDGE_VERSION, model_id(llm),
                        job["desc_hash"] or text_hash(clean), text_hash(profile))  # fmt: skip
        verdict: Judgement | None = None
        if cached := st.cache_get(key):
            verdict = Judgement.model_validate_json(cached)
            out.cache_hits += 1
        elif over_budget(st.usage_today(), budget):
            out.stopped_by_budget = True
            break
        else:
            posting = f"Title: {job['title']}\nLocation: {job['location']}\n{clean}"
            with usage_sink(st.add_usage):
                verdict, calls = judge(llm, f"{me}\n{data_block('posting', posting)}", JUDGE_STEP)
            out.calls += calls
            if verdict is None:  # outage or bad replies: leave the job for the next run
                out.failed = True
                break
            st.cache_put(key, JUDGE_STEP, verdict.model_dump_json())
        cap, why = verdict.score * 20, _clean_reason(verdict.reasons[0])
        score, reason = with_judge(job["score"], job["rank_reason"] or "", cap, why)
        db.save_judgement(
            job["id"], day=day, cap=cap, reason=why, score=score, rank_reason=reason,
            key=judge_key(job["desc_hash"], profile),
        )  # fmt: skip
        out.judged += 1
    return out


def rank_jobs(
    db: Database, st: JudgeStore, *, embedder: Embedder | None, llm: LLM | None,
    settings: Settings,
) -> str:  # fmt: skip
    """All tiers after a run; returns a one-line note for the run's tally."""
    log.info("ranking %s found jobs", "with the match model" if embedder else "on rules only")
    cheap = score_found(
        db, embedder, owner_years=settings.ranking.owner_years,
        max_new_embeddings=settings.ranking.embed_new_per_run,
    )  # fmt: skip
    note = f"ranked {cheap.scored} found jobs" + ("" if cheap.embedded else " (rules only)")
    if cheap.left_to_embed:
        note += f"; {cheap.left_to_embed} wait for the match model on later runs"
    log.info("%s", note)
    if llm is not None:
        j = judge_top(db, st, llm, top_n=settings.ranking.judge_top_n, budget=settings.llm)
        note += f"; AI judged {j.judged} ({j.calls} calls, {j.cache_hits} cached)"
        if j.stopped_by_budget:
            note += "; daily AI budget reached"
        if j.failed:
            note += "; AI judge failed, the rest wait for the next run"
    return note
