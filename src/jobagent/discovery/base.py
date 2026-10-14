"""Shared shapes for discovery adapters: companies, fetch results, the policy-gated loop."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import BaseModel, field_validator

from jobagent.models import Posting
from jobagent.policy import Policy

_TOKEN = re.compile(r"^[A-Za-z0-9_.-]+$")
_BLOCK_MARKERS = (
    "just a moment", "attention required", "access denied", "verify you are human",
    "complete the captcha", "are you a robot", "unusual traffic",
)  # fmt: skip


class Company(BaseModel):
    name: str
    ats: Literal["greenhouse", "lever", "ashby", "workable"]
    token: str
    region: Literal["us", "eu"] = "us"
    careers_host: str | None = None

    @field_validator("token")
    @classmethod
    def _safe_token(cls, value: str) -> str:
        if not _TOKEN.match(value):
            raise ValueError(f"unsafe board token: {value!r}")
        return value


def load_companies(path: Path) -> list[Company]:
    raw = yaml.safe_load(path.read_text()) or {}
    return [Company.model_validate(c) for c in raw.get("companies") or []]


Board = Literal[
    "jobgether", "hirist", "cutshort", "bebee", "switchly", "jobleads", "wellfound", "yc"
]
_TERM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .+-]{0,59}$")


class BoardSearch(BaseModel):
    """One search on a job board. `term` is a keyword or the board's role slug."""

    board: Board
    term: str

    @field_validator("term")
    @classmethod
    def _safe_term(cls, value: str) -> str:
        if not _TERM.match(value):
            raise ValueError(f"unsafe search term: {value!r}")
        return value.strip()

    @property
    def name(self) -> str:
        return f"{self.board}: {self.term}"

    @property
    def slug(self) -> str:
        return re.sub(r"[ .+]+", "-", self.term.lower()).strip("-")


def load_searches(path: Path) -> list[BoardSearch]:
    raw = yaml.safe_load(path.read_text()) or {}
    return [BoardSearch.model_validate(s) for s in raw.get("searches") or []]


@dataclass(frozen=True)
class FetchResult:
    status: int
    text: str
    url: str


class Fetcher(Protocol):
    def get(self, url: str) -> FetchResult: ...


@dataclass(frozen=True)
class Unreachable:
    company: str
    url: str
    reason: str


@dataclass
class DiscoveryResult:
    postings: list[Posting] = field(default_factory=list)
    unreachable: list[Unreachable] = field(default_factory=list)
    skipped_by_policy: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    per_target: dict[str, int] = field(default_factory=dict)
    portal: str = ""
    partial: bool = False  # some target read incompletely, so missing jobs prove nothing

    def extend(self, other: DiscoveryResult) -> None:
        self.postings += other.postings
        self.unreachable += other.unreachable
        self.skipped_by_policy += other.skipped_by_policy
        self.notes += other.notes
        self.per_target.update(other.per_target)
        self.portal = self.portal or other.portal
        self.partial = self.partial or other.partial


class Postings(list[Posting]):
    """One page's postings, plus the error names of records that could not be read."""

    def __init__(self) -> None:
        super().__init__()
        self.skipped: Counter[str] = Counter()
        self.more: bool | None = None  # the site's own "more results" flag, when it has one


def build_each[R](records: Iterable[R], build: Callable[[R], Posting | None]) -> Postings:
    """One bad record is counted and skipped; it never loses the rest of the page (R1)."""
    out = Postings()
    for record in records:
        try:
            posting = build(record)
        except Exception as exc:
            out.skipped[type(exc).__name__] += 1
            continue
        if posting is not None:
            out.append(posting)
    return out


def skipped_note(name: str, skipped: Counter[str]) -> str:
    total = sum(skipped.values())
    noun = "record" if total == 1 else "records"
    return f"{name}: {total} {noun} skipped ({', '.join(sorted(skipped))})"


def http_error(result: FetchResult) -> str | None:
    return f"http {result.status}" if result.status >= 400 else None


def blocked_reason(result: FetchResult, *, expect_json: bool) -> str | None:
    """A bot wall or error page is recorded, never worked around (KTD4)."""
    if error := http_error(result):
        return error
    if expect_json:
        try:
            json.loads(result.text)
        except ValueError:
            return "not json (possible bot wall)"
        return None
    head = result.text[:20000].lower()
    if any(marker in head for marker in _BLOCK_MARKERS):
        return "bot wall or captcha page"
    return None


class Target(Protocol):
    @property
    def name(self) -> str: ...


class Adapter[T: Target]:
    """Reads one target: a company's ATS board or a search on a job board."""

    portal: str = ""
    expect_json: bool = True
    # False when page_urls are separate lists (e.g. city pages), not one list split into pages.
    paged: bool = True
    page_size: int | None = None  # a last page this full means the page cap cut results

    def url_for(self, target: T) -> str:
        raise NotImplementedError

    def page_urls(self, target: T) -> list[str]:
        """Pages to read in order. Later 404s are skipped; if `paged`, an empty page ends it."""
        return [self.url_for(target)]

    def parse(self, text: str, target: T) -> list[Posting]:
        raise NotImplementedError

    def discover(self, target: T, fetcher: Fetcher, policy: Policy) -> DiscoveryResult:
        out = DiscoveryResult()
        decision = policy.begin(self.portal, "discover")
        if not decision.allowed:
            out.skipped_by_policy.append(f"{target.name}: {decision.reason}")
            return out
        seen: set[str] = set()
        skipped: Counter[str] = Counter()
        urls = self.page_urls(target)
        empty_first = truncated = False
        for page, url in enumerate(urls):
            if page and not (decision := policy.begin(self.portal, "discover")).allowed:
                why = "by kill switch" if policy.stopped() else f"({decision.reason})"
                out.notes.append(f"{target.name}: stopped {why} after page {page}")
                out.partial = True
                break
            try:
                result = fetcher.get(url)
            except Exception as exc:  # network and guard errors must not stop the run
                out.unreachable.append(
                    Unreachable(target.name, url, f"{type(exc).__name__}: {exc}")
                )
                break
            if page and result.status == 404:
                continue
            if error := http_error(result):
                out.unreachable.append(Unreachable(target.name, url, error))
                break
            # Wall markers count only on pages with no jobs; real pages may say "captcha" in CSS.
            try:
                parsed = self.parse(result.text, target)
            except Exception as exc:  # any layout change costs this target, never the run
                reason = blocked_reason(result, expect_json=self.expect_json)
                unparseable = f"unparseable: {type(exc).__name__}: {exc}"
                out.unreachable.append(Unreachable(target.name, url, reason or unparseable))
                break
            if isinstance(parsed, Postings):
                skipped += parsed.skipped
            if not parsed and (reason := blocked_reason(result, expect_json=self.expect_json)):
                out.unreachable.append(Unreachable(target.name, url, reason))
                break
            empty_first = empty_first or (page == 0 and not parsed)
            fresh = [p for p in parsed if p.url not in seen]
            if not fresh:
                if self.paged:
                    break  # past the last page of results
                continue
            seen.update(p.url for p in fresh)
            out.postings += fresh
            if page == len(urls) - 1:
                truncated = self._more_after(parsed)
        if skipped:
            out.notes.append(skipped_note(target.name, skipped))
        if empty_first and not out.postings:
            out.notes.append(f"{target.name}: 0 jobs (check layout)")
        if truncated:
            out.notes.append(f"{target.name}: truncated at {len(urls)} pages")
        out.partial = out.partial or bool(skipped or truncated or not out.postings)
        out.per_target[target.name] = len(out.postings)
        return out

    def _more_after(self, parsed: list[Posting]) -> bool:
        more = parsed.more if isinstance(parsed, Postings) else None
        if more is not None:
            return more
        return self.page_size is not None and len(parsed) >= self.page_size


def parse_json(text: str) -> Any:
    return json.loads(text)
