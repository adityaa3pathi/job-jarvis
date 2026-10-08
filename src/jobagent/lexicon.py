"""Word lists and matchers behind the truthfulness checker and the fact sheet loader."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from importlib import resources

SCOPE_SURFACES: dict[str, str] = {
    **dict.fromkeys(("led", "lead", "leads", "leading", "leader"), "lead"),
    **dict.fromkeys(("owned", "own", "owns", "owner", "owning", "ownership"), "own"),
    **dict.fromkeys(("architected", "architect", "architects", "architecting"), "architect"),
    **dict.fromkeys(("senior", "sr", "principal"), "senior"),
    **dict.fromkeys(("scaled", "scale", "scaling", "scales"), "scale"),
    **dict.fromkeys(("managed", "managing", "manages"), "manage"),
    **dict.fromkeys(("mentored", "mentoring", "mentors"), "mentor"),
    **dict.fromkeys(("spearheaded", "spearheading", "headed", "oversaw", "oversee"), "oversee"),
    **dict.fromkeys(("drove", "driven", "driving"), "drive"),
    **dict.fromkeys(("pioneered", "pioneering"), "pioneer"),
    **dict.fromkeys(("expert", "expertise", "advanced"), "expert"),
    "production": "production",
    "enterprise": "enterprise",
    "responsible": "responsible",
}
_SCOPE_PHRASES = {"end to end": "end_to_end", "end-to-end": "end_to_end", "team of": "team"}
_WORD = re.compile(r"[A-Za-z][A-Za-z']*")
NUMBER = re.compile(r"(?<![A-Za-z0-9.])\d[\d,]*(?:\.\d+)?[+%x]?(?![A-Za-z0-9])")
_UNITS = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty"
)
NUMBER_WORDS = {w: str(i) for i, w in enumerate(_UNITS.split())} | {
    "hundred": "100",
    "thousand": "1000",
    "dozen": "12",
}
NUMBER_WORDS.pop("one")  # also a pronoun, too noisy to treat as a number


def _word_file(name: str) -> frozenset[str]:
    return frozenset(resources.files("jobagent.data").joinpath(name).read_text().split())


KNOWN_TECH = _word_file("known_tech.txt")


@cache
def plain_words() -> frozenset[str]:
    return _word_file("plain_words.txt")


def words(text: str) -> set[str]:
    return {m.group(0).lower().removesuffix("'s").strip("'") for m in _WORD.finditer(text)}


def find_scope(text: str) -> set[str]:
    lowered = text.lower()
    found = {lemma for phrase, lemma in _SCOPE_PHRASES.items() if phrase in lowered}
    found |= {SCOPE_SURFACES[w] for w in words(text) if w in SCOPE_SURFACES}
    return found


@dataclass(frozen=True)
class Hit:
    id: str
    text: str
    start: int
    end: int


def canon_number(token: str) -> str:
    return token.replace(",", "").lower()


class Vocabulary:
    """Whole-token alias matcher. Longest alias wins, so Java never matches inside JavaScript."""

    def __init__(self, entries: list[tuple[str, list[str], bool]]) -> None:
        self._patterns: list[tuple[str, re.Pattern[str]]] = []
        flat = [(alias, sid, cs) for sid, aliases, cs in entries for alias in aliases]
        for alias, sid, case_sensitive in sorted(flat, key=lambda x: -len(x[0])):
            flags = 0 if case_sensitive else re.IGNORECASE
            body = re.escape(alias).replace(r"\ ", r"\s+")
            pattern = rf"(?<![A-Za-z0-9_]){body}(?![A-Za-z0-9_]|\.[A-Za-z0-9])"
            self._patterns.append((sid, re.compile(pattern, flags)))

    def find(self, text: str) -> list[Hit]:
        taken: list[tuple[int, int]] = []
        hits: list[Hit] = []
        for sid, pattern in self._patterns:
            for m in pattern.finditer(text):
                if any(m.start() < e and s < m.end() for s, e in taken):
                    continue
                taken.append((m.start(), m.end()))
                hits.append(Hit(sid, m.group(0), m.start(), m.end()))
        return sorted(hits, key=lambda h: h.start)


def blank_spans(text: str, hits: list[Hit]) -> str:
    chars = list(text)
    for hit in hits:
        for i in range(hit.start, hit.end):
            chars[i] = " "
    return "".join(chars)


def find_numbers(text: str) -> list[str]:
    """Digit numbers and number words, normalised (commas removed, lowercase)."""
    found = [canon_number(m.group(0)) for m in NUMBER.finditer(text)]
    found += [NUMBER_WORDS[w] for w in re.findall(r"[A-Za-z]+", text.lower()) if w in NUMBER_WORDS]
    return found
