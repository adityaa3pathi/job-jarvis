"""Normalisation used to decide that two postings are the same job."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Legal-form words only: "Technologies"/"Solutions" name different companies.
_LEGAL_SUFFIX = {
    "pvt", "private", "ltd", "limited", "inc", "incorporated", "llc", "llp", "corp",
    "corporation", "co", "plc", "gmbh",
}  # fmt: skip
_TRACKING_PARAMS = {
    "ref", "ref_src", "referrer", "source", "src", "gh_src", "lever-source", "lever-origin",
    "trk", "trkinfo", "trackingid", "refid", "fbclid", "gclid", "mc_cid", "mc_eid", "utm",
}  # fmt: skip
_WORK_MODE = r"remote|on-?site|hybrid|in-?office|wfh|work from home|anywhere"
_TITLE_NOISE = re.compile(rf"\(\s*(?:(?:{_WORK_MODE}|india|full[- ]?time)[\s,/&-]*)+\)")
_LOCATION_SPLIT = re.compile(r"[,/;|()]| - | or ")
_PLACE_SUFFIX = re.compile(r"\b(urban|district|division|rural)\b")
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
    """https, lowercase host without www, no fragment, trailing slash or tracking params."""
    raw = url.strip()
    if not raw:
        return ""
    parts = urlsplit(raw if "://" in raw else f"https://{raw}")
    host = parts.netloc.lower().removeprefix("www.")
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith("utm_")
    ]
    return urlunsplit(("https", host, parts.path.rstrip("/"), urlencode(query), ""))


def normalize_company(name: str) -> str:
    words = re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()
    while len(words) > 1 and words[-1] in _LEGAL_SUFFIX:
        words.pop()
    return " ".join(words)


def normalize_title(title: str) -> str:
    lowered = _TITLE_NOISE.sub(" ", title.lower())
    lowered = re.sub(r"[^a-z0-9+# ]", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def normalize_city(location: str) -> str:
    lowered = location.lower()
    for part in _LOCATION_SPLIT.split(lowered):
        part = re.sub(rf"\b({_WORK_MODE})\b", " ", part)
        part = re.sub(r"\s+", " ", _PLACE_SUFFIX.sub(" ", part)).strip(" -")
        if part and part != "india":
            return CITY_ALIASES.get(part, part)
    if re.search(rf"\b({_WORK_MODE})\b", lowered):
        return "remote"
    return "india" if "india" in lowered else ""


def dedupe_key(company: str, title: str, location: str, url: str = "") -> str:
    """Companyless postings key on their URL so unrelated jobs never merge."""
    norm_company = normalize_company(company)
    if not norm_company:
        return f"url:{normalize_url(url)}"
    return "|".join((norm_company, normalize_title(title), normalize_city(location)))
