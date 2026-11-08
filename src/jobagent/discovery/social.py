"""Shared rules for social posts (Telegram, Bluesky, Mastodon, Reddit): keep only real
tech hiring posts, drop people offering themselves, and drop the usual job scams first."""

from __future__ import annotations

import re
from datetime import date, timedelta

from jobagent.discovery.greenhouse import parse_date
from jobagent.models import Posting

HIRING = re.compile(
    r"\[hiring\]|\bwe(?:'|’)?re hiring\b|\bwe are hiring\b|\bnow hiring\b|\bis hiring\b|"
    r"\bhiring (?:for|an?|now|remote|a)\b|\blooking for (?:an? )?(?:\w+ ){0,3}"
    r"(?:developer|engineer|freelancer|dev|programmer)\b|\bneed (?:an? )?(?:\w+ ){0,2}"
    r"(?:developer|engineer|freelancer)\b|\bjob opening\b|\bopenings?\b|\bvacanc(?:y|ies)\b|"
    r"\bjoin our team\b|\bapply (?:now|here|at|via)\b|\bapply link\b|\bjob opportunity\b|"
    r"(?:^|\n)\s*roles?\b|\brole:\s|\bqualification\b",
    re.I,
)
FOR_HIRE = re.compile(
    r"\[for hire\]|\bfor hire\b|\bavailable for (?:work|hire|freelance|new projects)\b|"
    r"\bopen to work\b|#opentowork|\bhire me\b|\bi(?:'|’)?m looking for (?:a )?(?:job|work|role)",
    re.I,
)
# Sellers pitching to people who are hiring; dropped even when they say "looking for a developer".
SELF_PROMO = re.compile(
    r"\bif you(?:'|’)?re looking for\b|\bif you are looking for\b|\b(?:my|our) services\b|"
    r"\bwe offer\b|\blooking for (?:\w+ ){0,3}jobs\b",
    re.I,
)
TECH = re.compile(
    r"\b(?:python|node(?:\.?js)?|typescript|javascript|back[- ]?end|full[- ]?stack|fastapi|"
    r"django|react|llm|genai|gen ai|rag|machine learning|ai engineer|ml engineer|"
    r"software (?:engineer|developer)|developer|engineer|sde|programmer)\b",
    re.I,
)
# Telegram job channels in India carry a known scam wave (task jobs, fees, WhatsApp hand-offs).
SCAM = re.compile(
    r"\bregistration fee\b|\bsecurity deposit\b|\bpay (?:rs\.?|₹|inr)\s?\d|"
    r"\b(?:earn|income)\b[^.\n]{0,40}\b(?:daily|per day|a day|per hour|weekly)\b|"
    r"\b(?:task|typing|data entry|like and subscribe|youtube like)s? (?:job|work|based)\b|"
    r"\bwork from (?:home|mobile)\b[^.\n]{0,40}\bearn\b|\b(?:whatsapp|wa\.me)\b[^.\n]{0,30}"
    r"\b(?:message|text|contact|dm)\b|\bno (?:experience|interview) (?:needed|required)\b|"
    r"\binvest(?:ment)?\b|\bcrypto (?:trading|signals?)\b|\bpart[- ]time\b[^.\n]{0,30}\bearn\b",
    re.I,
)
# Header lines that say nothing about the role, so the next line becomes the title.
GENERIC = re.compile(
    r"^(?:job opportunity|new (?:remote )?job alert|job alert|we(?:'|’)?re hiring|we are hiring|"
    r"hiring|now hiring|urgent hiring|job update|new job|jobs?|apply asap|#?reshare|"
    r"company(?: name)?\b.*)\W*$",
    re.I,
)
IS_HIRING = re.compile(r"^(.{2,60}?) is hiring:? (.{4,})$", re.I)
COMPANY = re.compile(
    r"(?:^|\n)\W*company(?: name)?\s*[:\-–]?\s+([^\n|•━]{2,60})|"
    r"🏢\s*company(?: name)?\s*[:\-–]\s*([^\n|•━]{2,60})",
    re.I,
)
ROLE = re.compile(r"(?:^|\n)\W*(?:roles?|position|designation)\s*[:\-–]?\s+([^\n]{3,100})", re.I)
# A "Location:" line, so the India rule can drop posts tied to other countries.
WHERE = re.compile(r"(?:^|\n)\W*(?:location|job location|based in)\s*[:\-–]\s*([^\n]{2,80})", re.I)
MAX_AGE = timedelta(days=21)  # social hiring posts go stale fast; older ones are noise
MAX_TITLE = 120


def drop_reason(text: str) -> str | None:
    """Why a post is not a lead, or None to keep it. Scams are checked first."""
    if SCAM.search(text):
        return "scam_pattern"
    if SELF_PROMO.search(text):
        return "for_hire"
    if FOR_HIRE.search(text) and not HIRING.search(text.split("\n", 1)[0]):
        return "for_hire"
    if not HIRING.search(text):
        return "not_hiring"
    if not TECH.search(text):
        return "not_tech"
    return None


def title_of(text: str) -> str:
    """A "Role:" line if the post has one, else the first line that says something."""
    if role := ROLE.search(text):
        return " ".join(role.group(1).split())[:MAX_TITLE]
    for line in text.splitlines():
        clean = re.sub(r"\[(?:hiring|task)\]\s*", "", line, flags=re.I)
        clean = " ".join(re.sub(r"[^\w\s,.:/&()+#@$€£₹'’-]", " ", clean).split())
        if len(clean) >= 8 and not GENERIC.match(clean):
            hiring = IS_HIRING.match(clean)
            return (hiring.group(2) if hiring else clean)[:MAX_TITLE]
    return " ".join(text.split())[:MAX_TITLE] or "Social post"


def company_of(text: str, fallback: str) -> str:
    found = COMPANY.search(text)
    if found:
        return " ".join((found.group(1) or found.group(2)).split())[:80]
    first = " ".join((text.strip().splitlines() or [""])[0].split())
    hiring = IS_HIRING.match(first)
    return hiring.group(1)[:80] if hiring else fallback


def social_posting(
    *, source: str, url: str, text: str, fallback_company: str, posted: object = None
) -> Posting | None:
    """A Posting for a hiring post, or None when the post is not a lead."""
    when = parse_date(posted)
    if drop_reason(text) is not None or (when and date.today() - when > MAX_AGE):
        return None
    return Posting(
        source=source, url=url, company=company_of(text, fallback_company),
        title=title_of(text), description=text.strip()[:20_000], posted_date=when,
        location=" ".join(where.group(1).split()) if (where := WHERE.search(text)) else "",
    )  # fmt: skip
