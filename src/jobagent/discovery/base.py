"""Shared shapes for discovery adapters: companies, fetch results, the policy-gated loop."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NamedTuple, Protocol

import yaml
from pydantic import BaseModel, field_validator

from jobagent.models import Posting
from jobagent.policy import Policy

_TOKEN = re.compile(r"^[A-Za-z0-9_.-]+$")
_BLOCK_MARKERS = (
    "just a moment", "attention required", "access denied", "verify you are human",
    "complete the captcha", "are you a robot", "unusual traffic",
    "authwall", "sign up | linkedin", "sign in | linkedin", "linkedin login",
)  # fmt: skip
BOT_REFUSAL = 999  # LinkedIn's
RATE_LIMITED = 429
WALL = "bot wall or captcha page"


class Company(BaseModel):
    name: str
    ats: Literal["greenhouse", "lever", "ashby", "workable", "smartrecruiters"]
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
    "jobgether", "hirist", "cutshort", "bebee", "switchly", "jobleads", "wellfound", "yc",
    "linkedin", "himalayas", "remoteok", "jobicy", "remotive", "weworkremotely",
    "workingnomads", "hn", "instahyre",
]  # fmt: skip
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
    headers: Mapping[str, str] = field(default_factory=dict)  # etag and last-modified only


class Fetcher(Protocol):
    def get(self, url: str, headers: Mapping[str, str] | None = None) -> FetchResult: ...


class Validators(NamedTuple):
    """What a list URL looked like when last saved: one `fetch_cache` row (KTD3)."""

    etag: str | None
    last_modified: str | None
    body_sha256: str


def body_sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


DETAIL_CAP = 200  # job texts fetched one by one per run, when a list omits them


@dataclass
class SweepContext:
    """What one run knows before reading: validators per list URL, a check for job URLs
    already stored, and the shared budget for one-by-one job text fetches."""

    validators: Mapping[str, Validators] = field(default_factory=dict)
    known: Callable[[str], bool] = lambda url: False
    detail_cap: int = DETAIL_CAP
    details_used: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def take_detail(self) -> bool:
        """One slot of the per-run budget; portals run on several threads."""
        with self._lock:
            if self.details_used >= self.detail_cap:
                return False
            self.details_used += 1
            return True


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
    unchanged: list[str] = field(default_factory=list)  # targets not swept: same as last save
    fresh: dict[str, Validators] = field(default_factory=dict)  # to keep once the save works
    blocked: bool = False  # the site refused us: its other targets wait for the next run
    rate_limited: bool = False  # a 429: stops a search board, but one company board only

    def extend(self, other: DiscoveryResult) -> None:
        self.postings += other.postings
        self.unreachable += other.unreachable
        self.skipped_by_policy += other.skipped_by_policy
        self.notes += other.notes
        self.per_target.update(other.per_target)
        self.unchanged += other.unchanged
        self.fresh.update(other.fresh)
        self.portal = self.portal or other.portal
        self.partial = self.partial or other.partial
        self.blocked = self.blocked or other.blocked
        self.rate_limited = self.rate_limited or other.rate_limited


class Postings(list[Posting]):
    """One page's postings, plus the error names of records that could not be read."""

    def __init__(self) -> None:
        super().__init__()
        self.skipped: Counter[str] = Counter()
        self.more: bool | None = None  # the site's own "more results" flag, when it has one
        self.detail: dict[str, str] = {}  # posting URL -> its full text URL, when the list omits it


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
        return WALL
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
    conditional: bool = False  # one list URL that may answer 304: skip it when unchanged

    def url_for(self, target: T) -> str:
        raise NotImplementedError

    def page_urls(self, target: T) -> list[str]:
        """Pages to read in order. Later 404s are skipped; if `paged`, an empty page ends it."""
        return [self.url_for(target)]

    def parse(self, text: str, target: T) -> list[Posting]:
        raise NotImplementedError

    def parse_detail(self, text: str) -> str:
        """The job text from one job's own page, for lists that leave it out."""
        raise NotImplementedError

    def discover(
        self, target: T, fetcher: Fetcher, policy: Policy, ctx: SweepContext | None = None
    ) -> DiscoveryResult:
        out = DiscoveryResult()
        decision = policy.begin(self.portal, "discover")
        if not decision.allowed:
            out.skipped_by_policy.append(f"{target.name}: {decision.reason}")
            return out
        seen: set[str] = set()
        skipped: Counter[str] = Counter()
        urls = self.page_urls(target)
        cacheable = self.conditional and len(urls) == 1
        cached = ctx.validators.get(urls[0]) if ctx and cacheable else None
        validators: Validators | None = None
        empty_first = truncated = held_back = False
        for page, url in enumerate(urls):
            if page and not (decision := policy.begin(self.portal, "discover")).allowed:
                why = "by kill switch" if policy.stopped("discover") else f"({decision.reason})"
                out.notes.append(f"{target.name}: stopped {why} after page {page}")
                out.partial = True
                break
            try:
                result = fetcher.get(url, _ask_if_changed(cached)) if cached else fetcher.get(url)
            except Exception as exc:  # network and guard errors must not stop the run
                out.unreachable.append(
                    Unreachable(target.name, url, f"{type(exc).__name__}: {exc}")
                )
                break
            digest = body_sha256(result.text) if cacheable else ""
            if cached and (result.status == 304 or digest == cached.body_sha256):
                out.unchanged.append(target.name)  # not swept: no writes, no misses
                return out
            if page and result.status == 404:
                continue
            if error := http_error(result):
                out.unreachable.append(Unreachable(target.name, url, error))
                out.blocked = result.status == BOT_REFUSAL
                out.rate_limited = result.status == RATE_LIMITED
                break
            # Wall markers count only on pages with no jobs; real pages may say "captcha" in CSS.
            try:
                parsed = self.parse(result.text, target)
            except Exception as exc:  # any layout change costs this target, never the run
                reason = blocked_reason(result, expect_json=self.expect_json)
                unparseable = f"unparseable: {type(exc).__name__}: {exc}"
                out.unreachable.append(Unreachable(target.name, url, reason or unparseable))
                out.blocked = reason == WALL
                break
            if isinstance(parsed, Postings):
                skipped += parsed.skipped
            if not parsed and (reason := blocked_reason(result, expect_json=self.expect_json)):
                out.unreachable.append(Unreachable(target.name, url, reason))
                out.blocked = reason == WALL
                break
            empty_first = empty_first or (page == 0 and not parsed)
            if isinstance(parsed, Postings) and parsed.detail:
                held_back = self._fill_details(parsed, target, fetcher, policy, ctx, out)
            if cacheable:
                etag, modified = result.headers.get("etag"), result.headers.get("last-modified")
                validators = Validators(etag, modified, digest)
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
        if validators and not (out.partial or out.unreachable or held_back):
            out.fresh[urls[0]] = validators
        return out

    def _fill_details(
        self, parsed: Postings, target: T, fetcher: Fetcher, policy: Policy,
        ctx: SweepContext | None, out: DiscoveryResult,
    ) -> bool:  # fmt: skip
        """Fetch job text only for jobs not stored yet, within the run's cap. A job left
        without text is not kept, so the next run reads it; True when any was left."""
        ctx = ctx or SweepContext()
        kept: list[Posting] = []
        capped = failed = 0
        for posting in parsed:
            url = parsed.detail.get(posting.url)
            if url is None or ctx.known(posting.url):
                kept.append(posting)
            elif not ctx.take_detail():
                capped += 1
            elif not policy.begin(self.portal, "discover").allowed:
                failed += 1
            else:
                try:
                    result = fetcher.get(url)
                    if error := http_error(result):
                        raise ValueError(error)
                    text = self.parse_detail(result.text)
                except Exception:  # this job waits for the next run; the board goes on
                    failed += 1
                    continue
                kept.append(posting.model_copy(update={"description": text}))
        parsed[:] = kept
        if capped:
            out.notes.append(f"{target.name}: capped, {capped} new jobs left for the next run")
        if failed:
            out.notes.append(f"{target.name}: {failed} job texts could not be read")
        return bool(capped or failed)

    def _more_after(self, parsed: list[Posting]) -> bool:
        more = parsed.more if isinstance(parsed, Postings) else None
        if more is not None:
            return more
        return self.page_size is not None and len(parsed) >= self.page_size


def _ask_if_changed(cached: Validators) -> dict[str, str]:
    headers = {"If-None-Match": cached.etag} if cached.etag else {}
    return headers | ({"If-Modified-Since": cached.last_modified} if cached.last_modified else {})


def parse_json(text: str) -> Any:
    return json.loads(text)


def remote_at(where: str) -> str:
    """Remote-only boards: "Remote (USA)" lets the India rule drop it, "Remote (APAC)" not."""
    where = " ".join(where.split())
    if not where or where.lower() == "remote":
        return "Remote"
    return f"Remote ({where})"
