"""Truthfulness checker: nothing is sent unless every claim traces to the fact sheet (KTD5)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from pydantic import BaseModel

from jobagent.facts import FactSheet
from jobagent.lexicon import (
    KNOWN_TECH,
    NUMBER_WORDS,
    blank_spans,
    canon_number,
    find_numbers,
    find_scope,
    plain_words,
    words,
)
from jobagent.llm import LLM, LLMError, ask, data_block

Verdict = Literal["pass", "block", "unchecked"]
DEGREE_WORDS = frozenset(
    {"bachelor", "bachelors", "master", "masters", "mba", "phd", "doctorate", "diploma", "mtech"}
)
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+#_/'-]*")
_YEARS_Q = re.compile(r"(\d+)\s*\+?\s*(?:years?|yrs?)\b", re.IGNORECASE)
_MONTHS = (
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
)  # fmt: skip


@dataclass(frozen=True)
class Violation:
    code: str
    detail: str
    source: Literal["rule", "model"] = "rule"


@dataclass
class CheckReport:
    verdict: Verdict
    violations: list[Violation] = field(default_factory=list)
    model_status: str = "skipped"

    @property
    def blocked(self) -> bool:
        return self.verdict != "pass"

    @property
    def rule_violations(self) -> list[Violation]:
        return [v for v in self.violations if v.source == "rule"]


class ModelVerdict(BaseModel):
    verdict: Literal["pass", "block"]
    issues: list[str] = []


MODEL_SYSTEM = (
    "You audit a resume line against the facts it cites. Reply with "
    '{"verdict": "pass"|"block", "issues": [..]}. Block if the line claims more than the facts '
    'say: a changed role (for example "led" for "contributed"), invented impact, added '
    "ownership, or a different meaning, even when every tool name is correct."
)


@dataclass
class Allowed:
    skills: set[str]
    numbers: set[str]
    scope: set[str]
    words: set[str]
    text: str


def _context(sheet: FactSheet, cited: list[str], extra: Iterable[str]) -> Allowed:
    skills: set[str] = set()
    numbers: set[str] = set()
    scope: set[str] = set()
    texts: list[str] = []
    for fid in cited:
        if fid in sheet.bullets:
            b = sheet.bullets[fid]
            skills |= sheet.bullet_skill_ids(fid)
            scope |= set(b.scope_words)
            texts.append(b.text)
            numbers |= {canon_number(v) for n in b.numbers if sheet.numbers[n].confirmed
                        for v in sheet.numbers[n].values}  # fmt: skip
            parent = b.parent
            if parent in sheet.roles:
                r = sheet.roles[parent]
                texts += [r.employer, r.descriptor, r.location, *r.titles]
            elif parent in sheet.projects:
                texts += [*sheet.projects[parent].names, sheet.projects[parent].url]
        elif fid in sheet.skills:
            skills.add(fid)
            texts += [sheet.skills[fid].canonical, *sheet.skills[fid].aliases]
        elif fid in sheet.roles:
            r = sheet.roles[fid]
            texts += [r.employer, r.descriptor, r.location, r.start, r.end, *r.titles]
            numbers |= {part for d in (r.start, r.end) for part in re.findall(r"\d{4}", d)}
        elif fid in sheet.education:
            e = sheet.education[fid]
            texts += [e.degree, e.institution]
            numbers |= {str(e.start_year), str(e.end_year)}
        elif fid in sheet.projects:
            skills |= set(sheet.projects[fid].tech)
            texts += [*sheet.projects[fid].names, sheet.projects[fid].url]
        elif fid in sheet.numbers and sheet.numbers[fid].confirmed:
            numbers |= {canon_number(v) for v in sheet.numbers[fid].values}
    skills = {s for s in skills if sheet.skills[s].confirmed}
    allowed_words = set(plain_words()) | set(_MONTHS)
    for text in [*texts, *extra]:
        allowed_words |= words(text)
        for token in _TOKEN.findall(text):
            allowed_words |= {p.lower() for p in re.split(r"[-/._]", token) if p}
    return Allowed(skills, numbers, scope, allowed_words, " ".join(texts))


def check_citations(sheet: FactSheet, cited: list[str]) -> list[Violation]:
    if not cited:
        return [Violation("no_fact_id", "no fact id cited")]
    out = []
    for fid in cited:
        if not sheet.exists(fid):
            out.append(Violation("unknown_fact", fid))
        elif not sheet.is_confirmed(fid):
            out.append(Violation("unconfirmed_fact", fid))
    return out


def check_tools(sheet: FactSheet, text: str, ctx: Allowed) -> list[Violation]:
    out = []
    for hit in sheet.vocab.find(text):
        if hit.id not in ctx.skills:
            state = "" if sheet.skills[hit.id].confirmed else " (unconfirmed)"
            out.append(Violation("tool_not_cited", f"{hit.text}{state}"))
    return out


def _suspicious(part: str, sentence_start: bool) -> bool:
    lower = part.lower()
    if lower in KNOWN_TECH:
        return True
    if any(c.isdigit() for c in part) or any(c in part for c in "+#"):
        return True
    if any(c.isupper() for c in part[1:]):
        return True
    return part[0].isupper() and not sentence_start


def check_unknown_tokens(sheet: FactSheet, text: str, ctx: Allowed) -> list[Violation]:
    """Any technical or capitalised word outside the vocabulary and allowlist blocks."""
    blanked = blank_spans(text, sheet.vocab.find(text))
    out = []
    for m in _TOKEN.finditer(blanked):
        before = blanked[: m.start()].rstrip()
        start = not before or before[-1] in ".!?\n#"
        for part in re.split(r"[-/._]", m.group(0).rstrip(".,;:'")):
            part = part.removesuffix("'s").strip("'")
            if len(part) < 2 or part.isdigit() or part.lower() in ctx.words:
                start = False
                continue
            if re.fullmatch(r"[\d,.]+[+%x]?", part):
                continue
            if _suspicious(part, start):
                out.append(Violation("unknown_tool", part))
            start = False
    return out


def check_numbers(sheet: FactSheet, text: str, ctx: Allowed) -> list[Violation]:
    blanked = blank_spans(text, sheet.vocab.find(text))
    unconfirmed = {
        canon_number(v): n.id for n in sheet.numbers.values() if not n.confirmed for v in n.values
    }
    out = []
    for token in find_numbers(blanked):
        if token in ctx.numbers:
            continue
        code = "unconfirmed_number" if token in unconfirmed else "number_not_cited"
        out.append(Violation(code, token))
    # Glued forms like 2.5k are not numbers to the matcher; flag them too.
    for m in re.finditer(r"\b\d[\d,.]*[kKmMbB]\b", blanked):
        out.append(Violation("number_not_cited", m.group(0)))
    return out


def check_scope(text: str, ctx: Allowed) -> list[Violation]:
    return [Violation("scope_word", w) for w in sorted(find_scope(text) - ctx.scope)]


def check_degrees(text: str, ctx: Allowed) -> list[Violation]:
    found = {w for w in words(text) if w in DEGREE_WORDS}
    return [Violation("degree_not_cited", w) for w in sorted(found) if w not in ctx.words]


def rule_violations(
    sheet: FactSheet, text: str, cited: list[str], extra_allowed: Iterable[str] = ()
) -> list[Violation]:
    found = check_citations(sheet, cited)
    ctx = _context(sheet, [c for c in cited if sheet.exists(c)], extra_allowed)
    return [
        *found,
        *check_tools(sheet, text, ctx),
        *check_unknown_tokens(sheet, text, ctx),
        *check_numbers(sheet, text, ctx),
        *check_scope(text, ctx),
        *check_degrees(text, ctx),
    ]


def content_hash(content: str | bytes) -> str:
    data = content.encode() if isinstance(content, str) else content
    return hashlib.sha256(data).hexdigest()


class TruthChecker:
    def __init__(self, sheet: FactSheet, llm: LLM | None = None, today: date | None = None) -> None:
        self.sheet = sheet
        self.llm = llm
        self.today = today or date.today()

    def _model_pass(self, text: str, cited: list[str]) -> tuple[Verdict, list[Violation], str]:
        """The model can only add blocks. Any error leaves the result unchecked."""
        if self.llm is None:
            return "unchecked", [], "no model configured"
        user = (
            data_block(
                "facts", "\n".join(f"{f['id']}: {f['text']}" for f in self.sheet.llm_view(cited))
            )  # fmt: skip
            + "\n"
            + data_block("line", text)
        )
        try:
            result = ask(self.llm, ModelVerdict, system=MODEL_SYSTEM, user=user)
        except LLMError as exc:
            return "unchecked", [], str(exc)
        if result.verdict == "block":
            return (
                "block",
                [Violation("model_block", i, "model") for i in result.issues]
                or [Violation("model_block", "model flagged the line", "model")],
                "block",
            )
        return "pass", [], "pass"

    def check_unit(
        self,
        text: str,
        cited: list[str],
        *,
        extra_allowed: Iterable[str] = (),
        use_model: bool = True,
    ) -> CheckReport:
        found = rule_violations(self.sheet, text, cited, extra_allowed)
        if found:
            return CheckReport("block", found, "skipped")
        if not use_model:
            return CheckReport("pass", [], "skipped")
        verdict, violations, status = self._model_pass(text, cited)
        return CheckReport(verdict, violations, status)

    def check_answer(self, question: str, answer: str, cited: list[str]) -> CheckReport:
        """Typed answers: yes/no, option picks and values copied from a confirmed answer."""
        found = (
            check_citations(self.sheet, cited) if cited or answer.strip().lower() != "no" else []
        )
        norm = answer.strip().lower().rstrip(".")
        answer_facts = [self.sheet.answers[c] for c in cited if c in self.sheet.answers]
        if answer_facts:
            allowed = {
                v.strip().lower() for a in answer_facts for v in (a.value, *a.allowed_values)
            }
            if norm not in allowed:
                found.append(Violation("answer_mismatch", answer))
            return _finish(found)
        if norm in ("no", "false"):
            return _finish(found)
        if norm in ("yes", "true"):
            found += self._yes_claims(question, cited)
            return _finish(found)
        return _finish(found + rule_violations(self.sheet, answer, cited))

    def _yes_claims(self, question: str, cited: list[str]) -> list[Violation]:
        if not cited:
            return [Violation("no_fact_id", "yes needs a supporting fact")]
        ctx = _context(self.sheet, [c for c in cited if self.sheet.exists(c)], ())
        out = []
        asked = self.sheet.vocab.find(question)
        for hit in asked:
            if hit.id not in ctx.skills:
                out.append(Violation("tool_not_cited", hit.text))
        blanked = blank_spans(question, asked)
        for token in re.findall(r"[A-Za-z][A-Za-z0-9.+#]*", blanked):
            if token.lower() in KNOWN_TECH:
                out.append(Violation("unknown_tool", token))
        need = max((int(n) for n in _YEARS_Q.findall(question)), default=0)
        if need:
            have = (
                min(self.sheet.skill_years(h.id, self.today) for h in asked)
                if asked
                else self.sheet.experience_years(self.today)
            )
            if have < need:
                out.append(Violation("years_not_supported", f"asked {need}, evidence {have}"))
        return out


def _finish(found: list[Violation]) -> CheckReport:
    return CheckReport("block" if found else "pass", found, "skipped")


class NotApproved(Exception):
    pass


class ApprovalLedger:
    """Hashes of artifacts the checker approved in this run. Any later edit changes the hash."""

    def __init__(self) -> None:
        self._approved: set[str] = set()

    def approve(self, content: str | bytes) -> str:
        digest = content_hash(content)
        self._approved.add(digest)
        return digest

    def is_approved(self, content: str | bytes) -> bool:
        return content_hash(content) in self._approved

    def require(self, content: str | bytes) -> None:
        if not self.is_approved(content):
            raise NotApproved("content was not approved by the truthfulness checker in this run")


__all__ = [
    "ApprovalLedger",
    "CheckReport",
    "NotApproved",
    "NUMBER_WORDS",
    "TruthChecker",
    "Violation",
    "content_hash",
    "rule_violations",
]
