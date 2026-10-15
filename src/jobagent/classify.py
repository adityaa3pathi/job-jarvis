"""Rule-based role group, work mode and city of a job, for the Jobs page filters (R4, R6, R7)."""

from __future__ import annotations

import re

from jobagent.models import RoleGroup as Role
from jobagent.models import WorkMode
from jobagent.rules import _REMOTE, INDIA_MARKERS

# Checked in order: "React Native" is mobile, "Full Stack AI Engineer" is AI/ML.
ROLE_RULES: tuple[tuple[Role, str], ...] = (
    ("mobile", r"mobile|android|ios|react native|flutter|swiftui|swift"),
    ("ai_ml", r"ai|a\.i\.|ml|mlops|llms?|genai|gen ai|generative|machine learning|deep learning"
              r"|artificial intelligence|data scientist|nlp|computer vision|prompt engineer"
              r"|agentic"),
    ("frontend", r"front[- ]?end|ui (?:developer|engineer)"
                 r"|(?:react|angular|vue)(?:[ .]?js)? (?:developer|engineer)"),
    ("backend", r"back[- ]?end|server[- ]side|api (?:developer|engineer)|golang|django"
                r"|(?:node|python|java|go)(?:[ .]?js)? (?:developer|engineer)"),
    ("full_stack", r"full[- ]?stack|mern|mean stack"),
)  # fmt: skip
_ROLES = [(role, re.compile(rf"(?<![a-z0-9])(?:{rx})(?![a-z0-9])")) for role, rx in ROLE_RULES]

CITY_NAMES = {
    "bengaluru": "bangalore", "gurugram": "gurgaon", "cochin": "kochi", "mysuru": "mysore",
    "mangaluru": "mangalore", "trivandrum": "thiruvananthapuram", "ncr": "delhi",
}  # fmt: skip
_CITIES = sorted((m for m in INDIA_MARKERS if m != "india"), key=len, reverse=True)
_CITY = re.compile(rf"(?<![a-z])(?:{'|'.join(map(re.escape, _CITIES))})(?![a-z])")
_HYBRID = re.compile(r"\bhybrid\b", re.IGNORECASE)
_ONSITE = re.compile(r"\b(?:on[- ]?site|in[- ]office|work from office|wfo)\b", re.IGNORECASE)
_COUNTRY_ONLY = frozenset(("", "india", "in"))


def role_of(title: str) -> Role:
    lowered = title.lower()
    for role, rx in _ROLES:
        if rx.search(lowered):
            return role
    return "other"


def city_of(location: str) -> str:
    """The first India city named in the location, normalised; empty when none."""
    m = _CITY.search(location.lower())
    return CITY_NAMES.get(m.group(), m.group()) if m else ""


def work_mode_of(location: str, title: str) -> WorkMode:
    both = f"{location}\n{title}"
    if _HYBRID.search(both):
        return "hybrid"
    if _REMOTE.search(both):
        return "remote"
    if city_of(location) or _ONSITE.search(both):
        return "onsite"
    return "unknown" if location.strip().lower() in _COUNTRY_ONLY else "onsite"
