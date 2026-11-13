"""Suggested values for job-site form fields, drawn only from the owner's profile.
The browser extension reads the fields and fills a value only when the owner clicks."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from jobagent.answers import scrub
from jobagent.llm import LLM, ask, data_block
from jobagent.resume.evidence import cards_from_profile
from jobagent.resume.ontology import mention
from jobagent.resume.rewrite import new_tools, unsupported

Kind = Literal["text", "textarea", "number", "select", "radio", "checkbox", "chips", "date"]
# fact: backed by profile lines; saved: from the owner's saved answers; check: an AI
# suggestion to look over; you: a personal question left to the owner; none: no answer.
Status = Literal["fact", "saved", "check", "you", "none"]
MAX_FIELDS = 40
MAX_LINES = 200
MAX_PREFS = 30
CHOICE_KINDS = ("select", "radio")
FORM_VERSION = "form-v1"
GUESS_NOTE = "AI suggestion: check it before you submit."

# About the person, not the work: never drafted.
_PERSONAL = re.compile(
    r"gender|\bsex\b|pronoun|\bcaste\b|reservation|\b(?:sc|st|obc)\b|religio|ethnic|\brace\b|"
    r"disab|veteran|marital|married|date of birth|\bdob\b|\bage\b|lgbt|single parent|"
    r"father|mother|spouse",
    re.IGNORECASE,
)
_PERSONAL_CHOICE = re.compile(
    r"^(?:male|female|man|woman|non-?binary|transgender|prefer not to say|immigrant)$", re.I
)


def is_personal(field: FormField) -> bool:
    """Judged by the choices too: a gender question's label can be vague."""
    return bool(_PERSONAL.search(field.label)) or any(
        _PERSONAL.search(o) or _PERSONAL_CHOICE.match(o) for o in field.options
    )


# Choices the owner makes, not facts: an AI guess is allowed but always marked for checking.
_PREFERENCE = re.compile(
    r"salary|\bctc\b|compensation|stipend|\bpay\b|\blpa\b|notice|\bjoin|start date|availab|"
    r"relocat|location|\bcity|cities|commute|shift|work mode|remote|hybrid|on-?site|office|"
    r"language|visa|sponsor|authori[sz]|work permit|nationality|citizen|"
    r"why .*(?:join|leave|switch)",
    re.IGNORECASE,
)
_YES = re.compile(r"^(?:yes|true|i do|i have|y)\b", re.IGNORECASE)

SYSTEM = (
    "You fill in job-application and profile form fields for the candidate, in the first "
    "person. Use only the numbered profile lines and the saved preferences. Never add a tool, "
    "employer, title, degree, date or number they do not state. For each field: if profile "
    'lines answer it, set source "profile" and list the line numbers you used; if a saved '
    'preference answers it, set source "prefs"; for pay, notice period, location, availability '
    "or motivation questions with no saved preference, give a sensible suggestion with source "
    '"guess"; otherwise leave value empty with source "none". For select or radio fields the '
    "value must be exactly one of the options. For checkbox fields join the chosen options "
    'with " | ". For chips fields join items with ", ". Stay within max_len characters. Use '
    "the page text only to tailor wording to the job. Plain words, no emojis. Reply "
    '{"items": [{"id": str, "value": str, "source": str, "lines": [int]}]}.'
)


class FormField(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    label: str = Field(max_length=600)
    kind: Kind = "text"
    options: list[str] = Field(default_factory=list, max_length=80)
    max_len: int | None = Field(default=None, ge=1, le=20_000)

    @field_validator("options")
    @classmethod
    def _short_options(cls, value: list[str]) -> list[str]:
        return [o.strip()[:200] for o in value if o.strip()]


class FormPage(BaseModel):
    url: str = Field(default="", max_length=2_000)
    title: str = Field(default="", max_length=300)
    context: str = Field(default="", max_length=8_000)
    fields: list[FormField] = Field(min_length=1, max_length=MAX_FIELDS)
    prefs: dict[str, str] = Field(default_factory=dict)

    @field_validator("prefs")
    @classmethod
    def _small_prefs(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > MAX_PREFS:
            raise ValueError(f"at most {MAX_PREFS} saved answers")
        return {k.strip()[:80]: v.strip()[:300] for k, v in value.items() if v.strip()}


class _Item(BaseModel):
    id: str
    value: str = ""
    source: Literal["profile", "prefs", "guess", "none"] = "none"
    lines: list[int] = Field(default_factory=list)


class _Reply(BaseModel):
    items: list[_Item] = Field(default_factory=list)


@dataclass(frozen=True)
class Suggestion:
    id: str
    value: str
    status: Status
    note: str = ""


def profile_lines(summary: str, facts: list[str]) -> list[str]:
    lines = [ln.strip() for ln in [*summary.splitlines(), *facts] if ln.strip()]
    return [scrub(ln) for ln in lines[:MAX_LINES]]


def suggest(page: FormPage, summary: str, facts: list[str], llm: LLM) -> list[Suggestion]:
    """One model call for the page; every value then passes plain-code checks."""
    lines = profile_lines(summary, facts)
    personal = {f.id for f in page.fields if is_personal(f)}
    asked = [f for f in page.fields if f.id not in personal]
    by_id: dict[str, _Item] = {}
    if asked:
        prefs = "\n".join(f"{k}: {v}" for k, v in page.prefs.items())
        fields = json.dumps([f.model_dump(exclude_none=True) for f in asked], ensure_ascii=False)
        user = "\n".join([
            data_block("page", scrub(f"{page.title}\n{page.url}\n\n{page.context}")),
            data_block("profile", "\n".join(f"{i}: {ln}" for i, ln in enumerate(lines, 1))),
            data_block("prefs", prefs or "(none saved)"),
            data_block("fields", scrub(fields)),
        ])  # fmt: skip
        reply = ask(llm, _Reply, system=SYSTEM, user=user, step="form_fill", version=FORM_VERSION)
        by_id = {it.id: it for it in reply.items}
    allowed = {c.text for c in cards_from_profile(summary, facts) if c.kind == "skill"}
    out = []
    for f in page.fields:
        if f.id in personal:
            out.append(Suggestion(f.id, "", "you", "Personal question: answer it yourself."))
            continue
        item = by_id.get(f.id) or _Item(id=f.id)
        out.append(check(f, item, lines, page.prefs, allowed))
    return out


def check(
    field: FormField, item: _Item, lines: list[str], prefs: dict[str, str], allowed: set[str]
) -> Suggestion:
    value = _shape(field, item.value)
    if value is None:
        return Suggestion(field.id, "", "none", "Not one of the choices on the page.")
    if not value or item.source == "none":
        return Suggestion(field.id, "", "none", "Your profile doesn't answer this.")
    prefs_text = "\n".join(f"{k}: {v}" for k, v in prefs.items())
    if field.kind == "chips":
        return _chips(field, value, "\n".join([*lines, prefs_text]), allowed)
    if item.source == "prefs" and unsupported(prefs_text, value, set()) is None:
        return Suggestion(field.id, value, "saved")
    if item.source != "profile":
        if _PREFERENCE.search(field.label):
            return Suggestion(field.id, value, "check", GUESS_NOTE)
        return Suggestion(field.id, "", "none", "Your profile doesn't answer this.")
    cited = [lines[i - 1] for i in item.lines if 1 <= i <= len(lines)]
    if not cited:
        return Suggestion(field.id, "", "none", "No line in your profile backs this.")
    source = "\n".join([*cited, prefs_text])
    if field.kind in (*CHOICE_KINDS, "checkbox") and _YES.match(value):
        tools = new_tools(source, field.label, allowed)
        if tools:
            return Suggestion(field.id, "", "none", f"Your profile doesn't mention {tools[0]}.")
        return Suggestion(field.id, value, "fact")
    reason = unsupported(source, value, allowed)
    if reason == "new number":
        return Suggestion(field.id, value, "check", "Has a number your profile doesn't state.")
    if reason:
        return Suggestion(field.id, "", "none", f"Blocked: {reason} not in your profile.")
    return Suggestion(field.id, value, "fact")


def _chips(field: FormField, value: str, known_text: str, allowed: set[str]) -> Suggestion:
    """Each tag stands alone: kept only when the profile or saved answers name it."""
    items = list(dict.fromkeys(p.strip() for p in value.split(",") if p.strip()))
    known = {a.lower() for a in allowed}
    kept = [i for i in items if i.lower() in known or mention(i, known_text)]
    if not kept:
        return Suggestion(field.id, "", "none", "None of these are in your profile.")
    dropped = [i for i in items if i not in kept]
    note = f"Left out, not in your profile: {', '.join(dropped[:3])}." if dropped else ""
    return Suggestion(field.id, ", ".join(kept), "fact", note)


def _shape(field: FormField, raw: str) -> str | None:
    """Fit the value to the field; None when a choice matches no option."""
    value = raw.strip()
    if not value:
        return ""
    if field.kind in CHOICE_KINDS and field.options:
        return _option(field.options, value)
    if field.kind == "checkbox" and field.options:
        picked = [_option(field.options, part) for part in value.split("|") if part.strip()]
        return None if None in picked else " | ".join(p for p in picked if p)
    return _trim(value, field.max_len)


def _option(options: list[str], value: str) -> str | None:
    norm = value.strip().lower()
    return next((o for o in options if o.lower() == norm), None)


def _trim(value: str, limit: int | None) -> str:
    if not limit or len(value) <= limit:
        return value
    cut = value[:limit]
    if cut.endswith(".") and value[limit].isspace():
        return cut
    end = max(cut.rfind(". "), cut.rfind(".\n"))
    if end >= limit // 3:
        return cut[: end + 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > 0 else cut).rstrip()
