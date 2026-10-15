"""Fixed drop rules shared by storage (U2) and ranking (U6). No model calls here."""

from __future__ import annotations

import re

from jobagent.models import DropReason, Posting

RULES_VERSION = 3  # bump when a rule change could undrop or drop stored rows

INDIA_MARKERS = (
    "india", "bengaluru", "bangalore", "mumbai", "pune", "hyderabad", "chennai", "delhi",
    "gurgaon", "gurugram", "noida", "ghaziabad", "faridabad", "kolkata", "ahmedabad", "jaipur",
    "kochi", "cochin", "coimbatore", "indore", "chandigarh", "thiruvananthapuram", "nagpur",
    "lucknow", "bhubaneswar", "mysuru", "mysore", "vadodara", "surat", "ncr", "goa", "bhopal",
    "patna", "ranchi", "raipur", "guwahati", "visakhapatnam", "vijayawada", "madurai",
    "mangalore", "mangaluru", "nashik", "dehradun", "mohali", "gandhinagar", "trivandrum",
)  # fmt: skip
FOREIGN_MARKERS = (
    "united states", "usa", "us", "u.s.", "canada", "united kingdom", "uk", "london", "germany",
    "berlin", "france", "paris", "netherlands", "amsterdam", "ireland", "dublin", "spain",
    "singapore", "dubai", "uae", "saudi", "australia", "sydney", "new zealand", "europe",
    "emea", "latam", "brazil", "mexico", "japan", "tokyo", "china", "philippines",
    "new york", "san francisco", "california", "texas", "seattle", "toronto", "poland", "israel",
)  # fmt: skip
# Fee words alone are normal fintech text; spam asks the applicant to pay the employer.
SPAM_PATTERNS = (
    r"\bpay (?:a |an |the )?(?:\w+ )?(?:registration|processing|joining|training|application"
    r"|security) (?:fee|deposit|charges?)",
    r"\b(?:fee|deposit|charges?) of (?:rs\.?|inr|₹)\s*\d", r"\bpay to apply",
    r"\bearn\b \S+ ?(?:per|a) (?:day|week)", r"no experience required.{0,40}\bearn\b",
    r"data entry.{0,40}(?:per|/)\s*(?:day|hour)", r"guaranteed (?:job|placement)",
)  # fmt: skip
# Match the role itself, not benefits text like "unpaid leave" or "paid days off to volunteer".
UNPAID_PATTERNS = (
    r"\bunpaid (?:intern|internship|role|position|job|opportunity|work)\b",
    r"no stipend",
    r"(?<!leave )without (?:any )?(?:pay|stipend)",
    r"\bvolunteer (?:role|position|basis|opportunity|job)\b",
)
SENIOR_TITLE = re.compile(
    r"\b(senior|sr\.?|principal|staff|head|architect|manager|director|vp)\b"
    r"|\b(?:tech|team|technical|engineering|project|delivery|qa|test|module) lead\b"
    r"|\blead (?!gen)(?:\w+ )?(?:engineer|developer|scientist|architect|analyst|consultant"
    r"|designer|programmer)\b",
    re.IGNORECASE,
)
_NEGATED = re.compile(r"\([^)]*\bnot?\b[^)]*\)", re.IGNORECASE)
_CONTACT_VERBS = re.compile(
    r"\b(?:contact|message|msg|text|send|share|apply|resume|cv|call|ping|dm|reach)\b", re.I
)
# A real scam asks you to reach a number; a WhatsApp product JD names no contact.
_CONTACT_OBJECT = re.compile(
    r"\+\d{2}|\bwa\.me\b|\b\d{2}[\dx*]{6,}\b|\b\d{5}[\s-]\d{5}\b", re.IGNORECASE
)
_WHATSAPP = re.compile(r"whats\s?app", re.IGNORECASE)
_YEARS = re.compile(
    r"(\d{1,2})\s*(?:\+|[-–—]\s*(\d{1,2})|to\s*(\d{1,2}))?\s*\+?\s*(?:years?|yrs?)\b", re.I
)
_EXPERIENCE = re.compile(r"experience|\bexp\b", re.IGNORECASE)
_COMPANY_HISTORY = re.compile(
    r"\b(?:we have|we've|our (?:company|firm|founders?)|serving|in business|founded|since"
    r"|track record|legacy)\b",
    re.IGNORECASE,
)
OPEN_REGIONS = (
    "worldwide", "global", "globally", "anywhere in the world", "south asia", "asia", "apac",
    "asia pacific", "asia-pacific",
)  # fmt: skip
_LIST_SPLIT = re.compile(r"\s*[/|;]\s*|\s+or\s+")
_IN_CODE = re.compile(r"^remote\s*\((?:[^)]*;\s*)?in(?:\s*;[^)]*)?\)$")
_BARE_REMOTE = frozenset(
    ("remote", "full remote", "fully remote", "100% remote", "remote anywhere", "anywhere")
)
_REMOTE = re.compile(r"remote|anywhere", re.IGNORECASE)
_US_STATES = frozenset(
    ("AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN",
     "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
     "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
     "VT", "VA", "WA", "WV", "WI", "WY")
)  # fmt: skip
_US_STATE_SUFFIX = re.compile(r"([A-Za-z .]+),\s*([A-Z]{2})(?=\s*(?:$|[,;/|()-]))")
_INDIANA_CITIES = frozenset(
    ("indianapolis", "fort wayne", "evansville", "south bend", "carmel", "fishers",
     "bloomington", "lafayette", "west lafayette", "noblesville", "gary")
)  # fmt: skip


def _years_in(sentence: str) -> list[tuple[int, int | None, bool]]:
    found = []
    for m in _YEARS.finditer(sentence):
        high = m.group(2) or m.group(3)
        near = sentence[max(0, m.start() - 25) : m.end() + 25]
        found.append((int(m.group(1)), int(high) if high else None, bool(_EXPERIENCE.search(near))))
    return found


def parse_years(text: str, need_experience: bool = False) -> tuple[int, int | None] | None:
    """Lowest years asked, from phrases like '3-6 yrs' or '5+ years'.

    Prefers numbers next to 'experience' and skips company-history sentences.
    """
    found = []
    for sentence in re.split(r"[.;\n]", text):
        if _COMPANY_HISTORY.search(sentence):
            continue
        if need_experience and not _EXPERIENCE.search(sentence):
            continue
        found += _years_in(sentence)
    linked = [f for f in found if f[2]] or found
    if not linked:
        return None
    low, high, _ = min(linked, key=lambda f: f[0])
    return low, high


def years_asked(posting: Posting) -> tuple[int, int | None] | None:
    return parse_years(posting.experience_text) or parse_years(
        posting.description, need_experience=True
    )


def has_whatsapp_contact(text: str) -> bool:
    if re.search(r"\bwa\.me/", text, re.IGNORECASE):
        return True
    for m in _WHATSAPP.finditer(text):
        window = text[max(0, m.start() - 60) : m.end() + 60]
        if _CONTACT_VERBS.search(window) and _CONTACT_OBJECT.search(window):
            return True
    return False


def _has_marker(text: str, markers: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(re.search(rf"(?<![a-z]){re.escape(m)}(?![a-z])", lowered) for m in markers)


def _us_state_suffix(location: str) -> bool:
    for city, state in _US_STATE_SUFFIX.findall(location):
        if state in _US_STATES and (state != "IN" or city.strip().lower() in _INDIANA_CITIES):
            return True
    return False


def _open_to_india(location: str) -> bool:
    """Remote the owner can take from India: an open region, or a bare "Remote" list item."""
    items = [i.strip().lower() for i in _LIST_SPLIT.split(location)]
    if "in" in items or any(_IN_CODE.search(i) for i in items):
        return True  # YC writes India as a bare "IN" list item or inside "Remote (SG; IN)"
    if any(i in _BARE_REMOTE for i in items):
        return True
    return bool(_REMOTE.search(location)) and _has_marker(location, OPEN_REGIONS)


def outside_india(location: str) -> bool:
    if not location.strip():
        return False
    foreign = _has_marker(location, FOREIGN_MARKERS) or _us_state_suffix(location)
    return foreign and not _has_marker(location, INDIA_MARKERS) and not _open_to_india(location)


def check_hard_rules(posting: Posting, max_years_asked: int = 5) -> DropReason | None:
    text = f"{posting.title}\n{posting.description}"
    if posting.closed:
        return "closed"
    if has_whatsapp_contact(text):
        return "whatsapp_contact"
    if any(re.search(p, text, re.IGNORECASE) for p in SPAM_PATTERNS):
        return "spam"
    if any(re.search(p, text, re.IGNORECASE) for p in UNPAID_PATTERNS):
        return "unpaid"
    if outside_india(posting.location):
        return "outside_india"
    asked = years_asked(posting)
    if (asked and asked[0] >= max_years_asked) or SENIOR_TITLE.search(
        _NEGATED.sub("", posting.title)
    ):
        return "too_senior"
    return None
