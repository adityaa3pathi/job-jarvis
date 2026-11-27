"""Title filter for discovery (stage 4): is a job title one of the owner's target roles?

Target roles (owner, 27 Nov 2026): AI engineer, backend, full-stack and React Native.
Pure frontend React, .NET, Java, SAP, Salesforce, native iOS, QA, sales, recruiting, data
entry and other non-engineering roles are off-target, and so are internships and gigs.

Keywords decide first. A title with no keyword either way goes to the embedding step,
which drops it only when it is clearly closer to an off-target role than to any target
role, by more than `margin`. Anything borderline is kept: the ranking sees it later.
Off unless `discovery.title_filter` is on (config.yaml).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import NamedTuple

from jobagent.config import DiscoverySettings
from jobagent.index import Embedder
from jobagent.rules import RuleFilters


def _rx(*parts: str) -> re.Pattern[str]:
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(parts) + r")(?![a-z0-9])")


# Engineering work words, and target-domain words. Both kept titles; neither alone decides.
ROLE_ENG = _rx(
    r"engineer(?:s|ing)?", r"developers?", r"dev", r"sde(?: ?(?:[1-3]|i{1,3}))?", r"swe",
    r"programmer", r"architect", r"coder",
)  # fmt: skip
DOMAIN = _rx(
    r"ai", r"a\.i\.", r"artificial intelligence", r"ml", r"machine learning", r"llms?",
    r"gen ?ai", r"generative", r"agentic", r"rag", r"nlp", r"back[- ]?end",
    r"full[- ]?stack", r"fullstack", r"mern", r"mean stack", r"react[- ]native", r"python",
    r"django", r"fastapi", r"flask", r"node(?:\.?js)?", r"typescript", r"javascript", r"apis?",
    r"platform", r"software", r"sde", r"swe", r"server[- ]side",
)  # fmt: skip
# Stack words that rescue a title from the off-stack group below ("Java/Python Developer").
TARGET_STACK = _rx(
    r"python", r"node(?:\.?js)?", r"typescript", r"javascript", r"react[- ]native", r"ai",
    r"ml", r"llms?", r"gen ?ai", r"generative", r"agentic", r"fastapi", r"django",
)  # fmt: skip


class Group(NamedTuple):
    name: str
    pattern: re.Pattern[str]
    rescue: Callable[[str], bool]  # True keeps the title despite the match


def _never(_: str) -> bool:
    return False


def _eng_and_domain(t: str) -> bool:
    return bool(ROLE_ENG.search(t) and DOMAIN.search(t))


def _eng_or_domain(t: str) -> bool:
    return bool(ROLE_ENG.search(t) or DOMAIN.search(t))


def _target_stack(t: str) -> bool:
    return bool(TARGET_STACK.search(t))


def _target_domain(t: str) -> bool:
    """A target word other than the generic "software"/"platform"."""
    return bool(TARGET_STACK.search(t) or _BUILD.search(t))


_BUILD = _rx(r"back[- ]?end", r"full[- ]?stack", r"fullstack", r"apis?", r"server[- ]side")


# Word lists per group, as regex pieces. Edit freely: the eval shows what each change drops.
INTERNSHIP = (
    r"intern(?:ship)?s?", r"trainee", r"apprentice(?:ship)?", r"batch eligible",
    r"summer (?:analyst|associate)",
)  # fmt: skip
GIG = (
    r"ai trainer", r"data annotat\w*", r"annotator", r"freelance",
    r"(?<!smart )contract(?:or|ual)?", r"part[- ]time", r"계약직",
)  # fmt: skip
QA = (
    r"qa", r"q\.a\.", r"quality assurance", r"sdet", r"in test", r"testers?",
    r"test(?:ing)? (?:engineer|analyst|lead|automation)", r"automation test(?:ing|er)?",
    r"manual test(?:ing|er)?",
)  # fmt: skip
NON_ENGINEERING = (
    r"sales", r"marketing", r"account (?:executive|manager|management|director)",
    r"business development", r"bd[em]", r"recruiters?", r"recruiting",
    r"recruitment (?:specialist|partner|lead|manager|coordinator|executive|consultant)",
    r"talent acquisition", r"human resources?", r"hr", r"hrbp", r"hrm", r"payroll",
    r"accountant", r"accounting", r"finance", r"financial", r"fp&a", r"tax", r"legal",
    r"counsel", r"lawyer", r"attorney", r"paralegal", r"designer", r"graphic",
    r"program manager", r"programme manager", r"product manager", r"project manager",
    r"product owner", r"scrum master", r"customer (?:success|support|service|experience)",
    r"cx", r"cs", r"data entry", r"executive assistant", r"assistant", r"receptionist",
    r"copywriter", r"writer", r"editor", r"seo", r"social media", r"community manager",
    r"procurement", r"supply chain", r"logistics", r"warehouse", r"facilit(?:y|ies)", r"fleet",
    r"driver", r"merchandis\w*", r"buyer", r"category manager", r"teacher", r"tutor",
    r"faculty", r"nurse", r"pharmac\w*", r"seller support", r"operations?", r"ops",
)  # fmt: skip
OTHER_STACK = (
    r"java(?!script)", r"j2ee", r"spring ?boot", r"\w*\.net", r"dot ?net", r"c#", r"asp\.net",
    r"salesforce", r"sap", r"abap", r"mdm", r"ios", r"swift", r"objective-c",
)  # fmt: skip
FRONTEND = (
    r"front[- ]?end", r"frontend", r"ui (?:developer|engineer)", r"ui/ux",
    r"react(?:\.?js)? (?:developer|engineer|dev)", r"angular(?:js)?", r"vue(?:\.?js)?",
)  # fmt: skip
OTHER_ENGINEERING = (
    r"security", r"devops", r"devsecops", r"sre", r"site reliability", r"data engineer(?:ing)?",
    r"data scien\w*", r"database administrator", r"dba", r"network", r"embedded", r"firmware",
    r"hardware", r"vlsi", r"asic", r"fpga", r"cloud engineer", r"it support",
    r"desktop support", r"system administrator", r"sysadmin", r"support engineer",
    r"mainframe", r"mechanical", r"electrical", r"civil", r"chemical",
)  # fmt: skip
NON_ENGINEERING_WEAK = (
    r"managers?", r"management", r"directors?", r"head of", r"vp", r"vice president", r"chief",
    r"analysts?", r"analysis", r"consultant", r"specialist", r"coordinator", r"associate",
    r"strategy", r"strategist", r"partnerships?", r"governance", r"policy", r"administrator",
    r"admin", r"officer", r"representative", r"executive",
)  # fmt: skip

# Checked in order; the first group that matches and is not rescued drops the title.
GROUPS: tuple[Group, ...] = (
    Group("internship", _rx(*INTERNSHIP), _never),
    Group("gig", _rx(*GIG), _never),
    Group("qa", _rx(*QA), _never),
    # Strong words: only "<target word> engineer/developer" keeps the title.
    Group("non_engineering", _rx(*NON_ENGINEERING), _eng_and_domain),
    Group("other_stack", _rx(*OTHER_STACK), _target_stack),  # "Java/Python Developer" stays
    Group("frontend", _rx(*FRONTEND), _target_domain),  # "Frontend + Node" stays
    # Engineering, but not a target role: kept when a target word is also in the title.
    Group("other_engineering", _rx(*OTHER_ENGINEERING), _target_domain),
    # Weak words (manager, analyst...): any engineering or target word keeps the title.
    Group("non_engineering_weak", _rx(*NON_ENGINEERING_WEAK), _eng_or_domain),
)

TARGET_ROLES = (
    "AI engineer", "LLM engineer", "Generative AI engineer", "Machine learning engineer",
    "Backend engineer", "Python developer", "Node.js developer", "Full stack developer",
    "React Native developer", "Software engineer",
)  # fmt: skip
OFF_ROLES = (
    "Sales manager", "Marketing specialist", "Accountant", "HR specialist", "Recruiter",
    "Graphic designer", "Product manager", "Program manager", "Customer support executive",
    "Data entry operator", "Lawyer", "Operations manager", "Executive assistant",
    "Business analyst", "Warehouse supervisor", "Teacher",
)  # fmt: skip
DEFAULT_MARGIN = 0.05  # cosine; drop only when an off-target role wins by more than this


class Verdict(NamedTuple):
    off_target: bool
    why: str  # group name, "target", "embedding", "embedding: borderline" or "no keyword"


def keyword_verdict(title: str) -> Verdict | None:
    """The keyword call, or None when no keyword decides either way."""
    t = " ".join(title.lower().split())
    for group in GROUPS:
        if group.pattern.search(t) and not group.rescue(t):
            return Verdict(True, group.name)
    if ROLE_ENG.search(t) or DOMAIN.search(t):
        return Verdict(False, "target")
    return None


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))  # vectors come unit length


@dataclass
class TitleFilter:
    """Keywords, then embedding similarity with a margin. `make_embedder` is called once,
    on the first title no keyword decides; without it such titles are kept."""

    make_embedder: Callable[[], Embedder] | None = None
    margin: float = DEFAULT_MARGIN
    _embedder: Embedder | None = field(default=None, init=False, repr=False)
    _protos: tuple[list[list[float]], list[list[float]]] | None = field(default=None, init=False)
    _cache: dict[str, Verdict] = field(default_factory=dict, init=False, repr=False)

    def gap(self, title: str) -> float:
        """Best target-role similarity minus best off-target similarity."""
        if self._embedder is None:
            assert self.make_embedder is not None
            self._embedder = self.make_embedder()
            self._protos = (self._embedder.embed(list(TARGET_ROLES)),
                            self._embedder.embed(list(OFF_ROLES)))  # fmt: skip
        assert self._protos is not None
        vec = self._embedder.embed([title])[0]
        target, off = self._protos
        return max(_cos(vec, p) for p in target) - max(_cos(vec, p) for p in off)

    def verdict(self, title: str) -> Verdict:
        if title in self._cache:
            return self._cache[title]
        found = keyword_verdict(title)
        if found is None:
            if self.make_embedder is None:
                found = Verdict(False, "no keyword")
            else:
                gap = self.gap(title)
                borderline = "embedding: borderline" if gap < 0 else "embedding"
                found = (
                    Verdict(True, "embedding") if gap < -self.margin else Verdict(False, borderline)
                )
        self._cache[title] = found
        return found

    def off_target(self, title: str) -> bool:
        return self.verdict(title).off_target


def rule_filters(
    settings: DiscoverySettings, make_embedder: Callable[[], Embedder | None] | None = None
) -> RuleFilters | None:
    """The approved stage 4 filters from config, or None when all are off (rules as before).
    `make_embedder` (index.load_embedder) is called only when a title needs it; when it
    gives no model, such titles are kept."""
    if not (settings.title_filter or settings.region_words or settings.social_location):
        return None
    title = None
    if settings.title_filter:

        def embedder() -> Embedder:
            got = make_embedder() if make_embedder else None
            return got or _KeepAll()

        title = TitleFilter(embedder if make_embedder else None, settings.title_margin)
    return RuleFilters(
        title=title.off_target if title else None, region_words=settings.region_words,
        social_location=settings.social_location,
    )  # fmt: skip


class _KeepAll:
    """No model available: every title the keywords leave open scores as a target."""

    name = "keep-all"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] for _ in texts]
