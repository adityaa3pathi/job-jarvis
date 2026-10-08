"""Normalisation used to decide that two postings are the same job."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

_COMPANY_SUFFIX = re.compile(
    r"\b(pvt|private|ltd|limited|inc|incorporated|llc|llp|corp|corporation|co|technologies|"
    r"technology|solutions|services|india)\b"
)
CITY_ALIASES = {
    "bangalore": "bengaluru",
    "gurgaon": "gurugram",
    "bombay": "mumbai",
    "madras": "chennai",
    "new delhi": "delhi",
    "delhi ncr": "delhi",
    "ncr": "delhi",
    "cochin": "kochi",
}


def normalize_url(url: str) -> str:
    """Lowercase host, drop query, fragment and trailing slash."""
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def normalize_company(name: str) -> str:
    lowered = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    stripped = _COMPANY_SUFFIX.sub(" ", lowered)
    return re.sub(r"\s+", " ", stripped).strip() or re.sub(r"\s+", " ", lowered).strip()


def normalize_title(title: str) -> str:
    lowered = re.sub(r"\([^)]*\)", " ", title.lower())
    lowered = re.sub(r"[^a-z0-9+# ]", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def normalize_city(location: str) -> str:
    first = re.split(r"[,/;|(]| or ", location.lower())[0].strip()
    first = re.sub(r"\b(remote|on-?site|hybrid|in-?office)\b", "", first).strip(" -")
    return CITY_ALIASES.get(first, first)


def dedupe_key(company: str, title: str, location: str) -> str:
    return "|".join((normalize_company(company), normalize_title(title), normalize_city(location)))
