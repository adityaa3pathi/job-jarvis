"""The `job-agent tailor` command: local files and GitHub in, tailored resume and report out."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from jobagent.config import load_settings
from jobagent.llm import LLM, GeminiLLM, LLMError
from jobagent.resume.evidence import EvidenceCard, cards_from_summary, merge_cards
from jobagent.resume.github import (
    GitHubError,
    cards_from_snapshot,
    fetch_repo,
    make_client,
    parse_repo_ref,
)
from jobagent.resume.latex import LatexError
from jobagent.resume.pipeline import run_tailor


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--jd", type=Path, required=True, help="job description, text or HTML file")
    parser.add_argument("--latex", type=Path, required=True, help="your current resume .tex")
    parser.add_argument("--summary", type=Path, help="text file with your professional summary")
    parser.add_argument("--repo", action="append", default=[], help="GitHub repo URL; repeatable")
    parser.add_argument("--out", type=Path, default=Path("output"))
    parser.add_argument("--years", type=float, help="your years of experience, for the fit check")
    parser.add_argument("--no-gap-skills", action="store_true", help="never add unproven skills")


def build_llms() -> tuple[LLM, LLM]:
    model = load_settings().llm.model
    return GeminiLLM(model, temperature=0.5), GeminiLLM(model, temperature=0.0)


def collect_cards(summary: Path | None, repos: list[str]) -> list[EvidenceCard]:
    cards: list[EvidenceCard] = []
    if summary:
        cards += cards_from_summary(summary.read_text())
    if repos:
        with make_client(os.environ.get("GITHUB_TOKEN")) as client:
            for repo in repos:
                cards += cards_from_snapshot(fetch_repo(client, parse_repo_ref(repo)))
    return merge_cards(cards)


def run(args: argparse.Namespace) -> int:
    try:
        cards = collect_cards(args.summary, args.repo)
        if not cards:
            sys.stderr.write("No evidence. Give --summary and/or --repo.\n")
            return 2
        thinker, writer = build_llms()
        outcome = run_tailor(
            args.jd.read_text(), args.latex.read_text(), cards, thinker,
            writer_llm=writer, add_gaps=not args.no_gap_skills, years_have=args.years,
        )  # fmt: skip
    except (GitHubError, LatexError, LLMError, OSError) as exc:
        sys.stderr.write(f"Stopped: {type(exc).__name__}: {exc}\n")
        return 1
    slug = re.sub(r"[^a-z0-9]+", "-", outcome.profile.role_title.lower()).strip("-") or "resume"
    args.out.mkdir(parents=True, exist_ok=True)
    tex, report = args.out / f"{slug}.tex", args.out / f"{slug}-report.md"
    tex.write_text(outcome.tailored.latex)
    report.write_text(outcome.report)
    sys.stdout.write(f"resume: {tex}\nreport: {report}\n")
    return 0
