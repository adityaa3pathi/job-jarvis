"""Shared shapes for discovery adapters: companies, fetch results, the policy-gated loop."""

from __future__ import annotations

import json
import re
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
    "captcha", "are you a robot", "unusual traffic",
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

    def extend(self, other: DiscoveryResult) -> None:
        self.postings += other.postings
        self.unreachable += other.unreachable
        self.skipped_by_policy += other.skipped_by_policy


def blocked_reason(result: FetchResult, *, expect_json: bool) -> str | None:
    """A bot wall or error page is recorded, never worked around (KTD4)."""
    if result.status in (401, 403, 429, 503):
        return f"http {result.status}"
    if result.status >= 400:
        return f"http {result.status}"
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


class Adapter:
    portal: str = ""
    expect_json: bool = True

    def url_for(self, company: Company) -> str:
        raise NotImplementedError

    def parse(self, text: str, company: Company) -> list[Posting]:
        raise NotImplementedError

    def discover(self, company: Company, fetcher: Fetcher, policy: Policy) -> DiscoveryResult:
        out = DiscoveryResult()
        decision = policy.begin(self.portal, "discover")
        if not decision.allowed:
            out.skipped_by_policy.append(f"{company.name}: {decision.reason}")
            return out
        url = self.url_for(company)
        try:
            result = fetcher.get(url)
        except Exception as exc:  # network and guard errors must not stop the run
            out.unreachable.append(Unreachable(company.name, url, f"{type(exc).__name__}: {exc}"))
            return out
        reason = blocked_reason(result, expect_json=self.expect_json)
        if reason:
            out.unreachable.append(Unreachable(company.name, url, reason))
            return out
        try:
            out.postings = self.parse(result.text, company)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            out.unreachable.append(Unreachable(company.name, url, f"unparseable: {exc}"))
        return out


def parse_json(text: str) -> Any:
    return json.loads(text)
