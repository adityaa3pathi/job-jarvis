"""Build evals/jd_golden.yaml from a COPY of the jobs DB.
Usage: uv run python -m evals.make_jd_golden data/eval-copy.sqlite3"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import yaml

from jobagent.resume.jd_agent import _scan
from jobagent.sanitize import clean_posting_text

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evals" / "jd_golden.yaml"
MAX_CHARS = 2500
ROLES = ("ai_ml", "backend", "full_stack", "frontend", "mobile", "software", "other")
SOURCES = ("greenhouse", "ashby", "lever", "cutshort", "wellfound")
HEADER = """\
# JD extraction golden set (U16). Built by evals/make_jd_golden.py from a DB copy.
# OWNER: `expected` came from the code ontology scan, not a person. Check each list:
# delete plain-word hits the job does not ask for, add skills the scan missed.
"""


def trim(text: str) -> str:
    """Drop lines with contact details, then cut at a line end near MAX_CHARS."""
    text = "\n".join(ln for ln in text.splitlines() if "@" not in ln)
    if len(text) <= MAX_CHARS:
        return text
    cut = text.rfind("\n", 0, MAX_CHARS)
    return text[: cut if cut > MAX_CHARS // 2 else MAX_CHARS]


def pick(db: Path, count: int) -> list[dict[str, object]]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    chosen: list[dict[str, object]] = []
    # 7 roles and 5 sources are coprime, so this walks all 35 pairs, interleaved.
    pairs = [(ROLES[i % len(ROLES)], SOURCES[i % len(SOURCES)]) for i in range(35)]
    for offset, (role, source) in [(n, p) for n in (0, 1) for p in pairs]:
        if len(chosen) >= count:
            break
        row = conn.execute(
            "SELECT id, company, title, description FROM jobs WHERE role=? AND source=?"
            " AND length(description) > 1200 ORDER BY id LIMIT 1 OFFSET ?",
            (role, source, offset),
        ).fetchone()
        if row is None:
            continue
        text = trim(clean_posting_text(row[3])[0])
        found = _scan(text)
        if len(found) >= 3:
            chosen.append(
                {"id": f"{source}-{row[0]}", "source": source, "role": role,
                 "company": row[1], "title": row[2], "expected": found, "text": text}
            )  # fmt: skip
    return chosen


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("db", type=Path, help="a copy of the jobs DB, never the live one")
    parser.add_argument("--count", type=int, default=20)
    args = parser.parse_args()
    cases = pick(args.db, args.count)
    body = yaml.safe_dump({"cases": cases}, sort_keys=False, allow_unicode=True, width=100)
    OUT.write_text(HEADER + body)
    print(f"wrote {len(cases)} postings to {OUT}")


if __name__ == "__main__":
    main()
