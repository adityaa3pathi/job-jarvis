"""Shared data shapes for postings and their lifecycle."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

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
