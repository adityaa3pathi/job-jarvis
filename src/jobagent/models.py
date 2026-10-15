"""Shared data shapes for postings and their lifecycle."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Track = Literal["ai", "full_stack"]
DropReason = Literal[
    "outside_india",
    "too_senior",
    "whatsapp_contact",
    "closed",
    "spam",
    "unpaid",
    "no_track",
]
JobStatus = Literal[
    "found",
    "dropped",
    "ranked",
    "tailored",
    "waiting_for_owner",
    "applied",
    "replied",
    "interview",
    "rejected",
    "closed",
]


class Posting(BaseModel):
    """One job posting in the shape every discovery adapter returns."""

    source: str
    url: str
    company: str
    title: str
    location: str = ""
    experience_text: str = ""
    stack_words: list[str] = Field(default_factory=list)
    posted_date: date | None = None
    description: str = ""
    closed: bool = False


ListStatus = Literal["found", "applied", "interview", "rejected", "dropped", "closed"]
RoleGroup = Literal["ai_ml", "full_stack", "backend", "frontend", "mobile", "other"]
WorkMode = Literal["remote", "hybrid", "onsite", "unknown"]
DATE_WINDOWS = (1, 3, 7, 30)


class JobFilters(BaseModel):
    """The Jobs page filters; every set field narrows the list (R2-R10)."""

    status: ListStatus = "found"
    source: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,40}$")
    role: RoleGroup | None = None
    work_mode: WorkMode | None = None
    max_years: int | None = Field(default=None, ge=0, le=30)
    include_unstated: bool = True
    posted_within_days: int | None = None
    city: str | None = Field(default=None, max_length=60)
    company: str | None = Field(default=None, max_length=200)
    q: str | None = Field(default=None, max_length=60)
    sort: Literal["newest", "fewest_years"] = "newest"

    @field_validator("posted_within_days")
    @classmethod
    def _known_window(cls, days: int | None) -> int | None:
        if days is not None and days not in DATE_WINDOWS:
            raise ValueError(f"must be one of {DATE_WINDOWS}")
        return days
