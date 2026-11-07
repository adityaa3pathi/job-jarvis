"""Who to contact at a company: people named in the post, on the company site, YC founders,
and Hunter or Apollo when the owner asks. Nothing here sends anything or touches LinkedIn."""

from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel

from jobagent.discovery.base import Fetcher
from jobagent.discovery.fetch import KNOWN_ATS_HOSTS, KNOWN_BOARD_HOSTS
from jobagent.llm import short_cause
from jobagent.outreach.emails import (
    PERSONAL_HOSTS,
    ascii_words,
    domain_of,
    emails_in,
    guesses,
    is_role_inbox,
)
from jobagent.outreach.lookup import LookupError, PeopleLookup

log = logging.getLogger(__name__)
Source = Literal["post", "site", "yc", "hunter", "apollo", "manual"]
EmailSource = Literal["post", "site", "hunter", "apollo", "guess", "manual", ""]
SITE_PATHS = ("", "/contact", "/about", "/team")
_YC_JOB = re.compile(r"^/companies/([a-z0-9-]{1,100})(?:/|$)")
_PAGE_DATA = re.compile(r'data-page="([^"]+)"')
_URL = re.compile(r"https?://[^\s\"'<>)]+")
# Hosts that are never the employer's own site.
NOT_EMPLOYER = KNOWN_ATS_HOSTS | KNOWN_BOARD_HOSTS | {
    "linkedin.com", "github.com", "twitter.com", "x.com", "youtube.com", "facebook.com",
    "instagram.com", "medium.com", "glassdoor.com", "notion.so", "google.com", "forms.gle",
    "calendly.com", "ycombinator.com", "news.ycombinator.com", "bit.ly", "t.co", "lnkd.in",
}  # fmt: skip
# Shown first: someone who decides on engineering hires.
MANAGER_TITLE = re.compile(
    r"\b(cto|vp|head|director|founder|co-?founder|engineering manager|manager|lead)\b", re.I
)
RECRUITER_TITLE = re.compile(r"\b(recruit|talent|hiring|people|hr)\b", re.I)


class Candidate(BaseModel):
    name: str = ""
    title: str = ""
    email: str = ""
    email_source: EmailSource = ""
    other_emails: list[str] = []
    note: str = ""  # how sure the email is, in plain words
    linkedin_url: str = ""
    source: Source


class FindResult(BaseModel):
    domain: str
    candidates: list[Candidate]
    notes: list[str]


def _not_employer(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in NOT_EMPLOYER)


def guess_domain(company: str, texts: Iterable[str]) -> str:
    """The employer's own site, from links in the posting whose host names the company."""
    words = [w for w in ascii_words(company) if len(w) > 2]
    for text in texts:
        for url in _URL.findall(text or ""):
            host = domain_of(url)
            if host and not _not_employer(host) and any(w in host for w in words):
                return host
    return ""


def yc_slug(urls: Iterable[str]) -> str:
    for url in urls:
        parts = urlsplit(url)
        found = _YC_JOB.match(parts.path)
        if (parts.hostname or "").lower() == "www.ycombinator.com" and found:
            return found.group(1)
    return ""


def yc_company(fetcher: Fetcher, slug: str) -> dict[str, Any]:
    """Founders and website from the company's public YC page (as yc-outreach reads it)."""
    got = fetcher.get(f"https://www.ycombinator.com/companies/{slug}")
    match = _PAGE_DATA.search(got.text) if got.status == 200 else None
    if match is None:
        raise ValueError(f"YC page answered {got.status} without company data")
    company: dict[str, Any] = json.loads(html.unescape(match.group(1)))["props"]["company"]
    return company


def site_emails(fetcher: Fetcher, domain: str) -> list[str]:
    found: list[str] = []
    for path in SITE_PATHS:
        try:
            got = fetcher.get(f"https://{domain}{path}")
        except Exception as exc:  # one missing page is normal; the rest may still answer
            log.info("site page %s%s skipped: %s", domain, path, short_cause(exc))
            continue
        if got.status == 200:
            found += [e for e in emails_in(got.text, domain) if e not in found]
    return found


def _rank(c: Candidate) -> tuple[int, int, int]:
    """Named people first, deciders before recruiters, shared inboxes last."""
    inbox = bool(c.email) and is_role_inbox(c.email)
    who = 0 if MANAGER_TITLE.search(c.title) else 1 if RECRUITER_TITLE.search(c.title) else 2
    return (inbox, not c.name, who)


def _post_people(description: str, domain: str) -> list[Candidate]:
    """Emails written into the posting: their owner asked to be contacted."""
    out = []
    for email in emails_in(description):
        host = email.split("@")[1]
        if host in PERSONAL_HOSTS or not domain or host == domain or host.endswith("." + domain):
            note = "shared inbox in the post" if is_role_inbox(email) else "written in the post"
            out.append(Candidate(email=email, email_source="post", note=note, source="post"))
    return out


def find_contacts(
    *, company: str, description: str, urls: list[str], domain: str = "",
    board_fetcher: Fetcher, site_fetcher: Callable[[str], Fetcher],
    has_mx: Callable[[str], bool], lookups: Sequence[PeopleLookup] = (),
) -> FindResult:  # fmt: skip
    """Every free source, plus each paid lookup given. A source failing alone is only noted."""
    notes: list[str] = []
    found: list[Candidate] = []
    yc: dict[str, Any] = {}
    if slug := yc_slug(urls):
        try:
            yc = yc_company(board_fetcher, slug)
        except Exception as exc:
            notes.append(f"YC page not read: {short_cause(exc)}")
    domain = domain_of(domain) if domain else ""
    yc_site = domain_of(str(yc.get("website") or ""))
    domain = domain or yc_site or guess_domain(company, [description])
    found += _post_people(description, domain)
    if domain:
        try:
            emails = site_emails(site_fetcher(domain), domain)
            on_site = "on the company website"
            found += [
                Candidate(email=e, email_source="site", note=on_site, source="site") for e in emails
            ]
        except Exception as exc:
            notes.append(f"company site not read: {short_cause(exc)}")
    mail_ok = bool(domain) and has_mx(domain)
    if domain and not mail_ok:
        notes.append(f"{domain} has no mail server; email guesses are left out")
    for f in yc.get("founders") or []:
        name = str(f.get("full_name") or "")
        options = guesses(name, domain) if mail_ok else []
        found.append(Candidate(
            name=name, title=str(f.get("title") or "Founder"), email=options[0] if options else "",
            email_source="guess" if options else "", other_emails=options[1:],
            note="best guess (first@ is right about 80% of the time)" if options else "no email",
            linkedin_url=str(f.get("linkedin_url") or ""), source="yc",
        ))  # fmt: skip
    for lookup in lookups:
        try:
            got = lookup.people_at(domain=domain, company=company)
        except LookupError as exc:
            notes.append(str(exc))
            continue
        domain = domain or got.domain
        found += [
            Candidate(name=p.name, title=p.title, email=p.email, email_source=lookup.key,
                      note=p.note, linkedin_url=p.linkedin_url, source=lookup.key)
            for p in got.people
        ]  # fmt: skip
    if not domain:
        notes.append("company website unknown; type it in to search the site and guess emails")
    return FindResult(domain=domain, candidates=sorted(_dedupe(found), key=_rank), notes=notes)


def _dedupe(found: list[Candidate]) -> list[Candidate]:
    """One row per email (or per name without one); the better-sourced copy wins."""
    order = {"hunter": 0, "apollo": 0, "post": 1, "yc": 2, "site": 3, "manual": 4}
    best: dict[str, Candidate] = {}
    for c in sorted(found, key=lambda c: order[c.source]):
        key = c.email or c.name.lower()
        if key and key not in best:
            best[key] = c
    return list(best.values())
