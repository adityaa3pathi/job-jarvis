"""Stage 4 discovery filters: title, region words and social-post location. All off by
default, so the rules are unchanged until the owner switches one on."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest

from evals import run_discovery_eval
from jobagent.config import DiscoverySettings, Settings
from jobagent.db import Database
from jobagent.models import Posting
from jobagent.rules import (
    RuleFilters,
    check_hard_rules,
    location_from_text,
    outside_india,
    region_in_title,
)
from jobagent.targeting import TitleFilter, keyword_verdict, rule_filters


def posting(**kw: Any) -> Posting:
    base: dict[str, Any] = {
        "source": "bebee", "url": "https://bebee.com/in/jobs/x-1", "company": "Co",
        "title": "Backend Engineer", "location": "Bengaluru",
    }  # fmt: skip
    return Posting.model_validate(base | kw)


@pytest.mark.parametrize(
    "title",
    [
        "Senior Full Stack Developer", "Full Stack Developer - AI", "AI ENGINEER L1",
        "GEN AI / Agentic AI Engineer", "Senior Python Backend Developer – AWS (MTS IV)",
        "React Native Developer", "Software Engineer", "Sr Backend Developer",
        "Java/Python Developer", "Flex Recruitment 2026: Associate Developer IT, Coimbatore",
        "AI Engineer / AI Automation Specialist", "Director of Back-end Engineering",
        "Frontend + Node.js Engineer", "Node.js developer",
    ],
)  # fmt: skip
def test_target_titles_are_kept(title: str) -> None:
    verdict = keyword_verdict(title)
    assert verdict is not None and not verdict.off_target, verdict


@pytest.mark.parametrize(
    ("title", "group"),
    [
        ("Software Engineer Intern", "internship"),
        ("React Native Developer - AI Trainer", "gig"),
        ("Senior Software Development Engineer in Test", "qa"),
        ("Senior Account Executive - Inside Sales", "non_engineering"),
        ("Product Manager, AI", "non_engineering"),
        ("Java Backend Developer", "other_stack"),
        ("Senior.net Full Stack Developer", "other_stack"),
        ("Senior Software Engineer, Mobile (iOS)", "other_stack"),
        ("React Developer", "frontend"),
        ("Senior Security Engineer", "other_engineering"),
        ("Data Analysis Director", "non_engineering_weak"),
    ],
)  # fmt: skip
def test_off_target_titles_are_dropped_by_their_group(title: str, group: str) -> None:
    assert keyword_verdict(title) == (True, group)


class Fake:
    """Titles with "teach" sit near the off-target "Teacher" prototype, others near targets."""

    name = "fake"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 1.0] if "teach" in t.lower() else [1.0, 0.0] for t in texts]


def test_embedding_step_drops_only_past_the_margin() -> None:
    made: list[int] = []

    def make() -> Fake:
        made.append(1)
        return Fake()

    f = TitleFilter(make, margin=0.05)
    assert keyword_verdict("Teaching Fellow") is None  # no keyword decides it
    assert f.verdict("Teaching Fellow") == (True, "embedding")
    assert f.verdict("Member of Technical Staff") == (False, "embedding")  # a tie is kept
    assert TitleFilter(make, margin=2.0).off_target("Teaching Fellow") is False  # inside margin
    assert TitleFilter(None).verdict("Teaching Fellow") == (False, "no keyword")  # no model
    assert f.off_target("Backend Engineer") is False and len(made) == 2  # keywords: no model


def test_region_words_in_title_and_more_places() -> None:
    assert region_in_title("Senior SWE, Agentic AI (US, full-time)")
    assert region_in_title("Backend Engineer - US Remote")
    assert region_in_title("AI Engineer, EMEA")
    assert not region_in_title("Java Developer (US Shift)")
    assert not region_in_title("Join us as Backend Engineer")
    assert not region_in_title("Python Dev (India/US)")
    assert not region_in_title("Backend Engineer (EMEA)", "Remote (APAC, India)")
    assert not outside_india("Seoul, South Korea")  # rules v5
    assert outside_india("Seoul, South Korea", more=True)
    for india in ("Pune Division", "IN", "Karnataka", "maharashtra, india"):
        assert not outside_india(india, more=True)


def test_location_from_social_post_text() -> None:
    assert location_from_text("Clover is hiring a Senior SWE, Agentic AI (US, full-time).") == "US"
    assert location_from_text("Zoox is hiring 🌎 Foster City, California") == "california"
    assert location_from_text("Associate Developer – IT, Coimbatore. A US-based firm") == (
        "coimbatore"
    )
    assert location_from_text("Role: Backend Intern Batch Eligible: 2027") == ""
    open_to_all = location_from_text("A London startup hiring remote worldwide")
    assert not outside_india(open_to_all, more=True)


def test_filters_change_nothing_until_switched_on() -> None:
    korea = posting(title="Director, Category Growth Marketing", location="Seoul, South Korea")
    social = posting(
        source="bluesky", location="", title="Senior AI Engineer",
        description="HubSpot is hiring: Senior AI Engineer in Remote - United States.",
    )  # fmt: skip
    assert check_hard_rules(korea) is None and check_hard_rules(social) is None
    assert Settings().discovery == DiscoverySettings()
    assert rule_filters(Settings().discovery) is None
    assert check_hard_rules(korea, filters=RuleFilters(region_words=True)) == "outside_india"
    assert check_hard_rules(social, filters=RuleFilters(social_location=True)) == "outside_india"
    title = RuleFilters(title=TitleFilter(None).off_target)
    assert check_hard_rules(korea.model_copy(update={"location": "Bengaluru"}), filters=title) == (
        "off_target_title"
    )


def test_rule_filters_from_settings_without_a_model_keep_undecided_titles() -> None:
    on = DiscoverySettings(title_filter=True, region_words=True, social_location=True)
    filters = rule_filters(on, lambda: None)
    assert filters is not None and filters.title is not None
    assert filters.title("Sales Manager") and not filters.title("Teaching Fellow")


def test_database_saves_an_off_target_title_as_dropped() -> None:
    filters = rule_filters(DiscoverySettings(title_filter=True))
    db = Database(":memory:", filters=filters)
    sales, ai = posting(title="Sales Manager"), posting(url="https://bebee.com/in/jobs/x-2")
    out = db.upsert_many([sales, ai])
    assert [(r.status, r.drop_reason) for r in out] == [
        ("dropped", "off_target_title"), ("found", None),
    ]  # fmt: skip
    assert Database(":memory:").upsert_posting(sales).status == "found"  # filter off


def test_discovery_eval_scores_both_sides(tmp_path: Path) -> None:
    jobs = [
        {"id": 1, "source": "bebee", "title": "Python Developer", "location": "Pune"},
        {"id": 2, "source": "bebee", "title": "Sales Manager", "location": "Pune"},
        {"id": 3, "source": "greenhouse", "title": "HR Specialist", "location": "Seoul, Korea"},
        {"id": 4, "source": "bebee", "title": "Java Developer", "location": "Pune"},
    ]
    for j in jobs:
        j |= {"status": "found", "description": ""}
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({"jobs": jobs}))
    labels = tmp_path / "labels.csv"
    with labels.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "label", "sure"])
        w.writerows([[1, "yes", "yes"], [2, "no", "yes"], [3, "no", "yes"]])  # 4 unlabelled
    report = tmp_path / "r.json"
    args = ["--snapshot", str(snap), "--labels", str(labels), "--no-embed", "--report", str(report)]
    assert run_discovery_eval.main(args) == 0
    got = json.loads(report.read_text())
    assert got["baseline (rules v5)"]["total"] | {} == {
        "jobs": 3, "on": 1, "off": 2, "kept": 3, "off_kept": 2, "on_dropped": 0, "off_dropped": 0,
    }  # fmt: skip
    every = got["all three"]
    assert every["total"]["off_kept"] == 0 and every["total"]["on_dropped"] == 0
    assert [d["title"] for d in every["unlabelled_drops"]] == ["Java Developer"]
