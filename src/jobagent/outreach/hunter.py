"""Hunter.io domain search: people at a company with their work email. Called only on an
owner click; the free plan has about 50 credits a month."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

API = "https://api.hunter.io/v2/domain-search"
# Free plans cap limit+offset at 10; one page is all we read.
MAX_RESULTS = 10
DEPARTMENTS = "it,executive,management,hr"


class HunterError(Exception):
    """Hunter refused or could not be reached. The message never holds the API key."""


class HunterPerson(BaseModel):
    name: str
    title: str = ""
    email: str
    confidence: int = 0
    verified: bool = False
    linkedin_url: str = ""
    department: str = ""


class HunterResult(BaseModel):
    domain: str
    people: list[HunterPerson]


def _person(raw: dict[str, Any]) -> HunterPerson | None:
    email = str(raw.get("value") or "")
    name = " ".join(str(raw.get(k) or "") for k in ("first_name", "last_name")).strip()
    if not email or not name:
        return None
    check = raw.get("verification") or {}
    return HunterPerson(
        name=name, title=str(raw.get("position") or ""), email=email.lower(),
        confidence=int(raw.get("confidence") or 0), verified=check.get("status") == "valid",
        linkedin_url=str(raw.get("linkedin") or ""), department=str(raw.get("department") or ""),
    )  # fmt: skip


class Hunter:
    def __init__(
        self, api_key: str, *, transport: httpx.BaseTransport | None = None,
        timeout_s: float = 15.0,
    ) -> None:  # fmt: skip
        self._key = api_key
        self._client = httpx.Client(timeout=timeout_s, transport=transport)

    def domain_search(self, *, domain: str = "", company: str = "") -> HunterResult:
        """By domain when known, else by company name (Hunter then picks the domain)."""
        params: dict[str, str | int] = {"limit": MAX_RESULTS, "type": "personal",
                                        "department": DEPARTMENTS}  # fmt: skip
        params |= {"domain": domain} if domain else {"company": company}
        try:
            # The key goes in a header, so it never sits in a logged URL.
            res = self._client.get(API, params=params, headers={"X-API-KEY": self._key})
        except httpx.HTTPError as exc:
            raise HunterError(f"Hunter unreachable: {type(exc).__name__}") from None
        if res.status_code == 401:
            raise HunterError("Hunter rejected the API key")
        if res.status_code == 429:
            raise HunterError("Hunter limit reached (monthly credits or rate limit)")
        if res.status_code >= 400:
            raise HunterError(f"Hunter HTTP {res.status_code}")
        data = (res.json() or {}).get("data") or {}
        people = [p for raw in data.get("emails") or [] if (p := _person(raw))]
        return HunterResult(domain=str(data.get("domain") or domain), people=people)
