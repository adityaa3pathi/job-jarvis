"""Fixed drop rules shared by storage (U2) and ranking (U6). No model calls here."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from jobagent.models import DropReason, Posting

RULES_VERSION = 5  # bump when a rule change could undrop or drop stored rows
# v5 (27 Nov 2026, owner): a WhatsApp contact no longer drops a job.

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
# Read only with `discovery.region_words` on (stage 4, owner approval pending): more
# countries and cities seen in found jobs (Coupang's Seoul and Taipei passed as India).
MORE_REGIONS = (
    "americas", "north america", "south america", "latin america", "eu", "european union",
    "korea", "south korea", "seoul", "taiwan", "taipei", "taoyuan", "thailand", "bangkok",
    "vietnam", "ho chi minh", "hanoi", "indonesia", "jakarta", "malaysia", "kuala lumpur",
    "hong kong", "fukuoka", "osaka", "switzerland", "zurich", "geneva", "portugal", "lisbon",
    "italy", "milan", "rome", "sweden", "stockholm", "denmark", "copenhagen", "norway", "oslo",
    "finland", "helsinki", "belgium", "brussels", "austria", "vienna", "czech", "prague",
    "romania", "bucharest", "hungary", "budapest", "ukraine", "greece", "turkey", "istanbul",
    "egypt", "cairo", "nigeria", "lagos", "kenya", "nairobi", "south africa", "cape town",
    "argentina", "buenos aires", "colombia", "bogota", "chile", "santiago", "peru", "lima",
    "costa rica", "uruguay", "pakistan", "karachi", "lahore", "bangladesh", "dhaka",
    "sri lanka", "colombo", "nepal", "kathmandu", "qatar", "doha", "bahrain", "kuwait",
    "oman", "riyadh", "abu dhabi", "munich", "hamburg", "frankfurt", "warsaw", "krakow",
    "madrid", "barcelona", "boston", "chicago", "los angeles", "denver", "atlanta",
    "vancouver", "montreal", "melbourne", "auckland", "estonia", "tallinn", "lithuania",
    "latvia", "serbia", "croatia", "bulgaria", "slovakia", "slovenia", "luxembourg",
    "scotland", "edinburgh", "manchester", "cambridge, uk", "united states of america",
    "great britain", "england", "nordics", "dach", "benelux",
)  # fmt: skip
_CODES = r"(?:us|usa|u\.s\.|uk|eu|emea|latam|amer|americas|na|ca|anz)"
_NOT_PLACE = r"(?!\s*(?:shift|client|clients|hours|time|timezone|time zone|process|project|market))"
# Region codes only in a place-like spot: "(US, full-time)", "US Remote", "Remote - EMEA".
REGION_CODE = re.compile(
    rf"\(\s*{_CODES}\b{_NOT_PLACE}|\b{_CODES}[- ](?:only|based|remote|citizens?|residents?)\b"
    rf"|\bremote\s*[-,(/|:]?\s*{_CODES}\b{_NOT_PLACE}|\[\s*{_CODES}\s*\]",
    re.IGNORECASE,
)
_AMBIGUOUS = frozenset(("us", "uk", "eu", "u.s.", "na", "ca"))  # read as codes only, above
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
_YEARS = re.compile(
    r"(\d{1,2})\s*(?:\+|[-–—]\s*(\d{1,2})|to\s*(\d{1,2}))?\s*\+?\s*(?:years?|yrs?)\b", re.I
)
_EXPERIENCE = re.compile(r"experience|\bexp\b", re.IGNORECASE)
# "5+ years with Python", "at least 3 years in backend": a requirement even without the
# word "experience" nearby.
_ASKED_FOR = re.compile(
    r"^\s*\+?\s*(?:years?|yrs?)\s+(?:of\s+)?(?:professional\s+|hands-on\s+|commercial\s+"
    r"|industry\s+|relevant\s+|proven\s+)?(?:working\s+(?:with|in|on)|with|in|building|using"
    r"|developing|writing|programming|coding|of)\b",
    re.IGNORECASE,
)
_UNIT = re.compile(r"y(?:ea)?rs?", re.IGNORECASE)
_AT_LEAST = re.compile(r"(?:at least|minimum(?: of)?|min\.?|over|more than)\s*$", re.I)
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
}  # fmt: skip
_NUMBER_WORD = re.compile(
    r"\b(" + "|".join(_NUMBER_WORDS) + r")\b(?:\s*\(\d{1,2}\))?(?:-plus|\s+plus)?(?=[\s+-])", re.I
)
_DECADE = re.compile(r"\b(?:a|one) decade\b", re.IGNORECASE)
# The hard rules still read years the old way (digits next to "experience") until the
# owner approves dropping on the wider parse; ranking and filters use the wider one.
HARD_RULES_READ_WORDS = False
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


def _digits(sentence: str) -> str:
    """Number words as digits: "three years" -> "3 years", "five-plus" -> "5+",
    "a decade" -> "10 years"."""
    sentence = _DECADE.sub("10 years", sentence)

    def number(m: re.Match[str]) -> str:
        plus = "+" if "plus" in m.group(0).lower() else ""
        return f"{_NUMBER_WORDS[m.group(1).lower()]}{plus}"

    return _NUMBER_WORD.sub(number, sentence)


def _asked_for(sentence: str, m: re.Match[str]) -> bool:
    """'5+ years with X', 'at least 3 years in Y': a requirement on its own."""
    plus = "+" in m.group(0) or bool(_AT_LEAST.search(sentence[: m.start()]))
    unit = _UNIT.search(m.group(0))
    return (
        plus and unit is not None and bool(_ASKED_FOR.match(sentence[m.start() + unit.start() :]))
    )


def _years_in(sentence: str, words: bool = False) -> list[tuple[int, int | None, int]]:
    """(low, high, link) per years phrase. link 2: 'experience' is near; 1: worded as a
    requirement ('5+ years with X', only with `words`); 0: neither."""
    found = []
    for m in _YEARS.finditer(sentence):
        high = m.group(2) or m.group(3)
        near = sentence[max(0, m.start() - 25) : m.end() + 25]
        link = 2 if _EXPERIENCE.search(near) else 1 if words and _asked_for(sentence, m) else 0
        found.append((int(m.group(1)), int(high) if high else None, link))
    return found


def parse_years(
    text: str, need_experience: bool = False, words: bool = True
) -> tuple[int, int | None] | None:
    """Lowest years asked, from phrases like '3-6 yrs' or '5+ years'.

    Prefers numbers next to 'experience' and skips company-history sentences. With
    `words`, also reads number words ("three years", "a decade") and counts "5+ years
    with X" or "at least 3 years in Y" as asked even without the word "experience".
    """
    found = []
    for sentence in re.split(r"[.;\n]", text):
        if _COMPANY_HISTORY.search(sentence):
            continue
        if words:
            sentence = _digits(sentence)
        hits = _years_in(sentence, words)
        if need_experience and not _EXPERIENCE.search(sentence):
            hits = [h for h in hits if h[2]] if words else []
        found += hits
    # "5+ years of experience ... with 2+ years in closing": the experience number wins.
    best = max((f[2] for f in found), default=0)
    linked = [f for f in found if f[2] == best]
    if not linked:
        return None
    low, high, _ = min(linked, key=lambda f: f[0])
    return low, high


def years_asked(posting: Posting, words: bool = True) -> tuple[int, int | None] | None:
    return parse_years(posting.experience_text, words=words) or parse_years(
        posting.description, need_experience=True, words=words
    )


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


def outside_india(location: str, more: bool = False) -> bool:
    """Tied to another country and not open to India. `more` adds MORE_REGIONS."""
    if not location.strip():
        return False
    markers = FOREIGN_MARKERS + MORE_REGIONS if more else FOREIGN_MARKERS
    foreign = _has_marker(location, markers) or _us_state_suffix(location)
    return foreign and not _has_marker(location, INDIA_MARKERS) and not _open_to_india(location)


_FULL_NAMES = tuple(m for m in FOREIGN_MARKERS + MORE_REGIONS if m not in _AMBIGUOUS)


def region_in_title(title: str, location: str = "") -> bool:
    """A title tied to another region ("Backend Engineer (US Remote)", "AI Engineer, EMEA")
    whose title and location name neither India nor a region open to it."""
    named = bool(REGION_CODE.search(title)) or _has_marker(title, _FULL_NAMES)
    both = f"{title} | {location}"
    return named and not _has_marker(both, INDIA_MARKERS) and not _open_to_india(both)


SOCIAL_SOURCES = frozenset(("telegram", "bluesky", "mastodon", "reddit"))


def location_from_text(text: str) -> str:
    """Where a social post says the job is, for posts without a "Location:" line: India
    names win; otherwise the other places and any remote or open-region words it names."""
    found = [m for m in INDIA_MARKERS if _has_marker(text, (m,))]
    if found:
        return ", ".join(found[:3])
    places = [m for m in _FULL_NAMES if _has_marker(text, (m,))]
    places += [m.group(0).strip("()[] ") for m in REGION_CODE.finditer(text)][:2]
    if not places:
        return ""
    open_words = [m for m in OPEN_REGIONS if _has_marker(text, (m,))]
    remote = ["Remote"] if _REMOTE.search(text) else []
    return ", ".join(places[:3] + remote + open_words[:1])


@dataclass(frozen=True)
class RuleFilters:
    """Stage 4 discovery filters, each off until the owner approves it (`discovery.*` in
    config.yaml). `title` says True for an off-target title (targeting.TitleFilter)."""

    title: Callable[[str], bool] | None = None
    region_words: bool = False
    social_location: bool = False


def where_of(posting: Posting, filters: RuleFilters | None = None) -> str:
    """The location the India rule reads: a social post's text when its field is empty."""
    if (
        filters and filters.social_location and not posting.location.strip()
        and posting.source in SOCIAL_SOURCES
    ):  # fmt: skip
        return location_from_text(posting.description)
    return posting.location


def check_hard_rules(
    posting: Posting, max_years_asked: int = 6, filters: RuleFilters | None = None
) -> DropReason | None:
    text = f"{posting.title}\n{posting.description}"
    if posting.closed:
        return "closed"
    if any(re.search(p, text, re.IGNORECASE) for p in SPAM_PATTERNS):
        return "spam"
    if any(re.search(p, text, re.IGNORECASE) for p in UNPAID_PATTERNS):
        return "unpaid"
    more = bool(filters and filters.region_words)
    where = where_of(posting, filters)
    if outside_india(where, more) or (more and region_in_title(posting.title, where)):
        return "outside_india"
    asked = years_asked(posting, words=HARD_RULES_READ_WORDS)
    if asked and asked[0] >= max_years_asked:
        return "too_senior"
    if filters and filters.title and filters.title(posting.title):
        return "off_target_title"
    return None
