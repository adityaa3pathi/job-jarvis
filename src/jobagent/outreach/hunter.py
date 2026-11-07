"""Hunter.io domain search: people at a company with their work email. Called only on an
owner click; the free plan has about 50 credits a month."""

from __future__ import annotations

from typing import Any

import httpx

from jobagent.outreach.lookup import FoundPerson, LookupError, LookupResult

API = "https://api.hunter.io/v2/domain-search"
# Free plans cap limit+offset at 10; one page is all we read.
MAX_RESULTS = 10
DEPARTMENTS = "it,executive,management,hr"


def _person(raw: dict[str, Any]) -> FoundPerson | None:
    email = str(raw.get("value") or "")
    name = " ".join(str(raw.get(k) or "") for k in ("first_name", "last_name")).strip()
    if not email or not name:
        return None
    verified = (raw.get("verification") or {}).get("status") == "valid"
    sure = "Hunter verified" if verified else f"Hunter {int(raw.get('confidence') or 0)}% sure"
    return FoundPerson(
        name=name, title=str(raw.get("position") or ""), email=email.lower(), note=sure,
        linkedin_url=str(raw.get("linkedin") or ""),
    )  # fmt: skip


class Hunter:
    key = "hunter"

    def __init__(
        self, api_key: str, *, transport: httpx.BaseTransport | None = None,
        timeout_s: float = 15.0,
    ) -> None:  # fmt: skip
        self._key = api_key
        self._client = httpx.Client(timeout=timeout_s, transport=transport)

    def people_at(self, *, domain: str = "", company: str = "") -> LookupResult:
        """By domain when known, else by company name (Hunter then picks the domain)."""
        params: dict[str, str | int] = {"limit": MAX_RESULTS, "type": "personal",
                                        "department": DEPARTMENTS}  # fmt: skip
        params |= {"domain": domain} if domain else {"company": company}
        try:
            # The key goes in a header, so it never sits in a logged URL.
            res = self._client.get(API, params=params, headers={"X-API-KEY": self._key})
        except httpx.HTTPError as exc:
            raise LookupError(f"Hunter unreachable: {type(exc).__name__}") from None
        if res.status_code == 401:
            raise LookupError("Hunter rejected the API key")
        if res.status_code == 429:
            raise LookupError("Hunter limit reached (monthly credits or rate limit)")
        if res.status_code >= 400:
            raise LookupError(f"Hunter HTTP {res.status_code}")
        data = (res.json() or {}).get("data") or {}
        people = [p for raw in data.get("emails") or [] if (p := _person(raw))]
        return LookupResult(domain=str(data.get("domain") or domain), people=people)
