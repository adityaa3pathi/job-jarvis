"""Apollo.io: the people search is free but hides emails, so the top few deciders are then
revealed one by one (1 credit per email found). Free accounts need a work-email signup."""

from __future__ import annotations

import re
from typing import Any

import httpx

from jobagent.outreach.lookup import FoundPerson, LookupError, LookupResult

API = "https://api.apollo.io/api/v1"
SENIORITIES = ["owner", "founder", "c_suite", "vp", "head", "director", "manager"]
SEARCH_SIZE = 25
REVEAL = 3  # emails revealed per search: the credit cost of one owner click
# Credits go to people who decide on engineering hires first.
TECH = re.compile(
    r"\b(cto|engineer\w*|tech\w*|platform|founder|co-?founder|talent|recruit\w*)\b", re.I
)


class Apollo:
    key = "apollo"

    def __init__(
        self, api_key: str, *, transport: httpx.BaseTransport | None = None,
        timeout_s: float = 20.0, reveal: int = REVEAL,
    ) -> None:  # fmt: skip
        self._key, self.reveal = api_key, reveal
        self._client = httpx.Client(timeout=timeout_s, transport=transport)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            res = self._client.post(f"{API}{path}", json=body, headers={"x-api-key": self._key})
        except httpx.HTTPError as exc:
            raise LookupError(f"Apollo unreachable: {type(exc).__name__}") from None
        if res.status_code == 401:
            raise LookupError("Apollo rejected the API key")
        if res.status_code == 403:
            raise LookupError(
                "Apollo says this account cannot use the API"
                " (free accounts must sign up with a work email)"
            )
        if res.status_code == 429:
            raise LookupError("Apollo limit reached (credits or rate limit)")
        if res.status_code >= 400:
            raise LookupError(f"Apollo HTTP {res.status_code}")
        data: dict[str, Any] = res.json() or {}
        return data

    def people_at(self, *, domain: str, company: str = "") -> LookupResult:
        """Deciders at the domain, best first; only people with an email cost a credit."""
        if not domain:
            raise LookupError("Apollo needs the company website; type it in and search again")
        found = self._post("/mixed_people/api_search", {
            "q_organization_domains_list": [domain], "person_seniorities": SENIORITIES,
            "page": 1, "per_page": SEARCH_SIZE,
        })  # fmt: skip
        with_email = [p for p in found.get("people") or [] if p.get("has_email") and p.get("id")]
        with_email.sort(key=lambda p: not TECH.search(str(p.get("title") or "")))
        people: list[FoundPerson] = []
        for hit in with_email[: self.reveal]:
            person = self._post("/people/match", {"id": hit["id"]}).get("person") or {}
            email = str(person.get("email") or "").lower()
            if not email or "email_not_unlocked" in email:
                continue
            status = str(person.get("email_status") or "unverified")
            people.append(FoundPerson(
                name=str(person.get("name") or hit.get("first_name") or ""),
                title=str(person.get("title") or hit.get("title") or ""), email=email,
                note=f"Apollo {status}",
                linkedin_url=str(person.get("linkedin_url") or ""),
            ))  # fmt: skip
        return LookupResult(domain=domain, people=people)
