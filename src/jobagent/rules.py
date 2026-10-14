"""Fixed drop rules shared by storage (U2) and ranking (U6). No model calls here."""

from __future__ import annotations

import re

from jobagent.models import DropReason, Posting

INDIA_MARKERS = (
    "india", "bengaluru", "bangalore", "mumbai", "pune", "hyderabad", "chennai", "delhi",
    "gurgaon", "gurugram", "noida", "ghaziabad", "faridabad", "kolkata", "ahmedabad", "jaipur",
    "kochi", "cochin", "coimbatore", "indore", "chandigarh", "thiruvananthapuram", "nagpur",
    "lucknow", "bhubaneswar", "mysuru", "mysore", "vadodara", "surat", "ncr",
)  # fmt: skip
FOREIGN_MARKERS = (
    "united states", "usa", "us", "u.s.", "canada", "united kingdom", "uk", "london", "germany",
    "berlin", "france", "paris", "netherlands", "amsterdam", "ireland", "dublin", "spain",
    "singapore", "dubai", "uae", "saudi", "australia", "sydney", "new zealand", "europe",
    "emea", "apac", "latam", "brazil", "mexico", "japan", "tokyo", "china", "philippines",
    "new york", "san francisco", "california", "texas", "seattle", "toronto", "poland", "israel",
)  # fmt: skip
SPAM_PATTERNS = (
    r"registration fee", r"security deposit", r"pay to apply", r"processing fee",
    r"earn \S+ ?(?:per|a) (?:day|week)", r"no experience required.{0,40}earn",
    r"data entry.{0,40}(?:per|/)\s*(?:day|hour)", r"guaranteed (?:job|placement)",
)  # fmt: skip
# Match the role itself, not benefits text like "unpaid leave" or "paid days off to volunteer".
UNPAID_PATTERNS = (
    r"\bunpaid (?:intern|internship|role|position|job|opportunity|work)\b",
    r"no stipend",
    r"without (?:any )?(?:pay|stipend)",
    r"\bvolunteer (?:role|position|basis|opportunity|job)\b",
)
SENIOR_TITLE = re.compile(
    r"\b(senior|sr\.?|lead|principal|staff|head|architect|manager|director|vp)\b", re.IGNORECASE
)
_CONTACT_VERBS = r"(?:contact|message|msg|text|send|share|apply|resume|cv|call|ping|dm|reach)"
_WHATSAPP = re.compile(
    rf"(?:{_CONTACT_VERBS}.{{0,50}}whats\s?app|whats\s?app.{{0,50}}{_CONTACT_VERBS})|wa\.me/",
    re.IGNORECASE | re.DOTALL,
)
_YEARS = re.compile(r"(\d{1,2})\s*(?:\+|-\s*(\d{1,2})|to\s*(\d{1,2}))?\s*(?:years?|yrs?)\b", re.I)


def parse_years(text: str) -> tuple[int, int | None] | None:
    """Lowest and highest years asked, from phrases like '3-6 yrs' or '5+ years'."""
    match = _YEARS.search(text)
    if not match:
        return None
    low = int(match.group(1))
    high_raw = match.group(2) or match.group(3)
    return low, int(high_raw) if high_raw else None


def years_asked(posting: Posting) -> tuple[int, int | None] | None:
    found = parse_years(posting.experience_text)
    if found:
        return found
    for sentence in re.split(r"[.\n]", posting.description):
        if re.search(r"experience|exp\b", sentence, re.IGNORECASE):
            found = parse_years(sentence)
            if found:
                return found
    return None


def _has_marker(text: str, markers: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(re.search(rf"(?<![a-z]){re.escape(m)}(?![a-z])", lowered) for m in markers)


def outside_india(location: str) -> bool:
    if not location.strip():
        return False
    return _has_marker(location, FOREIGN_MARKERS) and not _has_marker(location, INDIA_MARKERS)


def check_hard_rules(posting: Posting, max_years_asked: int = 5) -> DropReason | None:
    text = f"{posting.title}\n{posting.description}"
    if posting.closed:
        return "closed"
    if _WHATSAPP.search(text):
        return "whatsapp_contact"
    if any(re.search(p, text, re.IGNORECASE) for p in SPAM_PATTERNS):
        return "spam"
    if any(re.search(p, text, re.IGNORECASE) for p in UNPAID_PATTERNS):
        return "unpaid"
    if outside_india(posting.location):
        return "outside_india"
    asked = years_asked(posting)
    if (asked and asked[0] >= max_years_asked) or SENIOR_TITLE.search(posting.title):
        return "too_senior"
    return None
