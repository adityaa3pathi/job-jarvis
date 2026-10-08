"""Finds which tools a posting asks for, split into known skills and gaps."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jobagent.facts import FactSheet
from jobagent.lexicon import KNOWN_TECH, blank_spans


@dataclass
class Requirements:
    skills: dict[str, str] = field(default_factory=dict)  # skill id -> surface form in posting
    unknown: list[str] = field(default_factory=list)  # tools named but absent from the fact sheet

    @property
    def confirmed_ids(self) -> set[str]:
        return set(self.skills)


def extract_requirements(sheet: FactSheet, text: str) -> Requirements:
    """Deterministic only: a model never decides what the posting asks for."""
    req = Requirements()
    hits = sheet.vocab.find(text)
    for hit in hits:
        req.skills.setdefault(hit.id, hit.text)
    seen: set[str] = set()
    for token in re.findall(r"[A-Za-z][A-Za-z0-9.+#-]*", blank_spans(text, hits)):
        token = token.rstrip(".-")
        if token.lower() in KNOWN_TECH and token.lower() not in seen:
            seen.add(token.lower())
            req.unknown.append(token)
    return req
