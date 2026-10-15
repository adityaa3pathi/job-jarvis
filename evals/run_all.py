"""AI-step evals (U16): JD extraction, rewrite refusals, prompt injection.
Usage: uv run python -m evals.run_all [--live] [--no-history]
--live calls the real model and spends quota: owner only, before a prompt or model change."""

from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from jobagent.llm import LLM, StubLLM, model_id
from jobagent.resume import coach, jd_agent
from jobagent.resume.coach import build_report
from jobagent.resume.jd_agent import analyze_jd
from jobagent.resume.match import match_profile
from jobagent.resume.ontology import canonical
from jobagent.resume.rewrite import check_rewrite
from jobagent.sanitize import clean_posting_text
from jobagent.web.store import IST

HERE = Path(__file__).resolve().parent
HISTORY = HERE / "history.csv"
DIGEST = "Backend engineer, 3 years of Python, FastAPI and PostgreSQL."
_SEEN = re.compile(r"Known terms seen in the text \(.*?\): (.*?)\. Include", re.S)
_DATA = re.compile(r'<data name="(\w+)">\n(.*?)\n</data>', re.S)
_ASK_SKILL = re.compile(r"\b(?:add|include|list|mark) (\w+)", re.I)
_ABOUT_PERSON = re.compile(r"\b(?:candidate|applicant|person)\b", re.I)


def _blocks(user: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in _DATA.finditer(user)}


def stub_responder(system: str, user: str) -> dict[str, Any]:
    """A gullible model: obeys any instruction it can still see, so leaks show up."""
    blocks = _blocks(user)
    if "job_posting" in blocks:
        text = blocks["job_posting"]
        seen = _SEEN.search(user)
        names = [n.strip() for n in seen.group(1).split(",")] if seen else []
        names += _ASK_SKILL.findall(text)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        duties = [ln for ln in lines if _ABOUT_PERSON.search(ln)] + lines[1:4]
        return {
            "role_title": lines[0] if lines else "", "level": "mid", "years_min": None,
            "domain": "software", "responsibilities": duties,
            "skills": [{"name": n, "importance": "must"} for n in names if n],
            "implicit_skills": [],
        }  # fmt: skip
    if "MATCH" in blocks:
        job, match = json.loads(blocks["JOB"]), json.loads(blocks["MATCH"])
        echoed = [r for r in job.get("responsibilities", []) if _ABOUT_PERSON.search(r)]
        good = [*match.get("proven", []), *echoed]
        return {"fit": 3, "summary": "Stub report.", "good": good, "learn": [], "risks": []}
    return {}


def stub_llm() -> StubLLM:
    return StubLLM(stub_responder)


def _load(name: str) -> list[dict[str, Any]]:
    return list(yaml.safe_load((HERE / name).read_text())["cases"])


# --- (a) JD extraction ---


@dataclass
class JDResult:
    recall: float
    precision: float
    cases: list[dict[str, Any]] = field(default_factory=list)


def jd_eval(llm: LLM, cases: list[dict[str, Any]] | None = None) -> JDResult:
    hits = expected_n = got_n = 0
    rows = []
    for case in cases if cases is not None else _load("jd_golden.yaml"):
        expected = {canonical(s).lower() for s in case["expected"]}
        got = {s.name.lower() for s in analyze_jd(case["text"], llm).skills}
        both = len(expected & got)
        hits, expected_n, got_n = hits + both, expected_n + len(expected), got_n + len(got)
        rows.append(
            {"id": case["id"], "missed": sorted(expected - got), "extra": sorted(got - expected)}
        )
    return JDResult(hits / max(expected_n, 1), hits / max(got_n, 1), rows)


# --- (b) rewrite refusals: reports the check's rates, never changes the check ---


@dataclass
class RewriteResult:
    refused_adversarial: float
    accepted_honest: float
    misses: list[str] = field(default_factory=list)


def rewrite_eval(cases: list[dict[str, Any]] | None = None) -> RewriteResult:
    bad = good = refused = accepted = 0
    misses = []
    for case in cases if cases is not None else _load("rewrite_cases.yaml"):
        reason = check_rewrite(case["original"], case["new"], set(case["allowed"]))
        if case["expect"] == "refuse":
            bad += 1
            refused += reason is not None
        else:
            good += 1
            accepted += reason is None
        if (reason is not None) != (case["expect"] == "refuse"):
            misses.append(case["id"])
    return RewriteResult(refused / max(bad, 1), accepted / max(good, 1), misses)


# --- (c) prompt injection ---


@dataclass
class InjectionResult:
    passed: float
    failed: list[str] = field(default_factory=list)
    known_gaps: list[str] = field(default_factory=list)


def _leaks(markers: list[str], text: str) -> list[str]:
    return [m for m in markers if re.search(rf"\b{re.escape(m)}\b", text, re.I)]


def injection_case(case: dict[str, Any], llm: LLM) -> bool:
    clean, flagged = clean_posting_text(case["html"])
    profile = analyze_jd(case["html"], llm)
    report = build_report(profile, match_profile(profile, []), DIGEST, llm)
    outputs = json.dumps([asdict(profile), json.loads(report.to_json())])
    leaked = _leaks(case["markers"], clean + "\n" + outputs)
    return not leaked and (flagged or case["expect"] != "flag")


def injection_eval(llm: LLM, cases: list[dict[str, Any]] | None = None) -> InjectionResult:
    cases = cases if cases is not None else _load("injection_cases.yaml")
    failed, gaps = [], []
    for case in cases:
        if not injection_case(case, llm):
            (gaps if case.get("known_gap") else failed).append(case["id"])
    gated = [c for c in cases if not c.get("known_gap")]
    return InjectionResult((len(gated) - len(failed)) / max(len(gated), 1), failed, gaps)


# --- runner ---


def run_all(llm: LLM, history: Path | None = HISTORY) -> dict[str, Any]:
    jd, rw, inj = jd_eval(llm), rewrite_eval(), injection_eval(llm)
    row = {
        "date_ist": datetime.now(UTC).astimezone(IST).strftime("%d-%m-%Y %H:%M IST"),
        "model": model_id(llm),
        "jd_prompt": jd_agent.PROMPT_VERSION,
        "coach_prompt": coach.PROMPT_VERSION,
        "jd_recall": round(jd.recall, 3),
        "jd_precision": round(jd.precision, 3),
        "rewrite_refused_adversarial": round(rw.refused_adversarial, 3),
        "rewrite_accepted_honest": round(rw.accepted_honest, 3),
        "injection_passed": round(inj.passed, 3),
        "injection_known_gaps": len(inj.known_gaps),
    }
    if history is not None:
        new = not history.exists()
        with history.open("a", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(row))
            if new:
                writer.writeheader()
            writer.writerow(row)
    return {"row": row, "jd": jd, "rewrite": rw, "injection": inj}


def _print(result: dict[str, Any]) -> None:
    width = max(len(k) for k in result["row"])
    for key, value in result["row"].items():
        print(f"{key.ljust(width)}  {value}")
    print(f"\nrewrite misses: {', '.join(result['rewrite'].misses) or 'none'}")
    print(f"injection failures: {', '.join(result['injection'].failed) or 'none'}")
    print(f"injection known gaps: {', '.join(result['injection'].known_gaps) or 'none'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="real model; spends quota")
    parser.add_argument("--no-history", action="store_true")
    args = parser.parse_args(argv)
    llm: LLM = stub_llm()
    if args.live:
        from jobagent.config import load_settings
        from jobagent.llm import GeminiLLM

        llm = GeminiLLM.from_settings(load_settings().llm, 0.0)
    _print(run_all(llm, None if args.no_history else HISTORY))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
