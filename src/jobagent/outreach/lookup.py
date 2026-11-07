"""Paid people-lookup services (Hunter, Apollo) share one shape, so the finder treats them
alike. Each is called only on an owner click: both spend monthly credits."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class LookupError(Exception):  # noqa: A001
    """A lookup service refused or could not be reached. The message never holds a key."""


class FoundPerson(BaseModel):
    name: str
    title: str = ""
    email: str
    note: str = ""  # how sure the service is, in plain words
    linkedin_url: str = ""


class LookupResult(BaseModel):
    domain: str
    people: list[FoundPerson]


class PeopleLookup(Protocol):
    key: str  # "hunter" or "apollo": the candidate's source

    def people_at(self, *, domain: str, company: str) -> LookupResult: ...
