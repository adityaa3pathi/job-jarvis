"""Export a fixed pool of found jobs for the owner to label yes/no (stage 2 ranking eval).

Read-only: opens Turso directly and runs SELECTs only (no Database(), which migrates).

The pool is stratified by the live web score so one set of labels gives both numbers:
  top     the web's "best" sort, first TOP_N rows (what the owner sees first)
  band80  a random sample of the other found jobs scoring 80+ (the fit bar, 4 of 5 x 20)
  band60  a random sample of jobs scoring 60-79
  low     a random sample of jobs scoring under 60
Each stratum's population size is saved so recall can be weighted back to the whole list.

Writes two files to OUT_DIR (outside the repo, which is public):
  label-pool-<date>.csv      the sheet to label: fill the `label` column with yes or no
  label-pool-<date>.json     full posting text, profile text and marks at export time

Usage: TURSO_DATABASE_URL=... TURSO_AUTH_TOKEN=... uv run python evals/export_label_pool.py
       [--out DIR] [--seed N]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import libsql

IST = timezone(timedelta(hours=5, minutes=30))
DAY = "substr(COALESCE(NULLIF(j.posted_date, ''), j.seen_date), 1, 10)"
BEST = f"j.score IS NULL, j.score DESC, {DAY} DESC, j.id DESC"  # db.ORDERS["best"]
STRATA = (  # name, SQL condition on j.score, how many to take
    ("top", None, 30),
    ("band80", "j.score >= 80", 30),
    ("band60", "j.score >= 60 AND j.score < 80", 25),
    ("low", "j.score < 60", 15),
)
COLUMNS = (
    "j.id, j.company, j.title, j.location, j.experience_text, j.stack_words, j.description,"
    " j.posted_date, j.seen_date, j.source, j.role, j.work_mode, j.min_years, j.max_years,"
    " j.desc_hash, j.score, j.rank_reason, j.judge_score, j.judge_reason, j.judge_key,"
    " (SELECT url FROM job_links l WHERE l.job_id=j.id ORDER BY rowid LIMIT 1) AS url"
)
SHORT_CHARS = 280


def rows(conn: Any, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cur = conn.execute(sql, args)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def short(text: str | None) -> str:
    flat = re.sub(r"\s+", " ", text or "").strip()
    if not flat:
        return "(no description saved)"
    return flat if len(flat) <= SHORT_CHARS else flat[:SHORT_CHARS].rsplit(" ", 1)[0] + " ..."


def build_pool(conn: Any, seed: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rng = random.Random(seed)
    found = "j.status='found' AND j.score IS NOT NULL"
    top_n = STRATA[0][2]
    best = f"SELECT {COLUMNS} FROM jobs j WHERE {found} ORDER BY {BEST} LIMIT ?"
    top = rows(conn, best, (top_n,))
    taken = {r["id"] for r in top}
    for r in top:
        r["stratum"] = "top"
    pool, sizes = list(top), {"top": len(top)}
    for name, cond, n in STRATA[1:]:
        ids = [
            r["id"]
            for r in rows(conn, f"SELECT j.id FROM jobs j WHERE {found} AND {cond} ORDER BY j.id")
            if r["id"] not in taken
        ]
        sizes[name] = len(ids)
        pick = sorted(rng.sample(ids, min(n, len(ids))))
        marks = ",".join("?" * len(pick))
        got = rows(conn, f"SELECT {COLUMNS} FROM jobs j WHERE j.id IN ({marks})", tuple(pick))
        for r in got:
            r["stratum"] = name
        pool += got
        taken |= set(pick)
    return pool, sizes


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path.home() / "job-jarvis-labels")
    ap.add_argument("--seed", type=int, default=20261127)
    args = ap.parse_args()
    url, token = os.environ["TURSO_DATABASE_URL"], os.environ["TURSO_AUTH_TOKEN"]
    conn = libsql.connect(url, auth_token=token)
    pool, sizes = build_pool(conn, args.seed)
    profile = rows(conn, "SELECT * FROM profile")
    marks = rows(conn, "SELECT * FROM labels ORDER BY job_id")
    found_total = rows(conn, "SELECT COUNT(*) AS n FROM jobs WHERE status='found'")[0]["n"]

    day = datetime.now(IST).date().isoformat()
    args.out.mkdir(parents=True, exist_ok=True, mode=0o700)
    sheet = args.out / f"label-pool-{day}.csv"
    snap = args.out / f"label-pool-{day}.json"
    order = list(pool)
    random.Random(args.seed + 1).shuffle(order)  # so the sheet isn't read in score order
    with sheet.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["n", "label", "title", "company", "location", "score", "link",
                    "short_description", "job_id"])  # fmt: skip
        for n, r in enumerate(order, 1):
            w.writerow([n, "", r["title"], r["company"], r["location"], r["score"],
                        r["url"] or "", short(r["description"]), r["id"]])  # fmt: skip
    meta = {
        "exported_at": datetime.now(UTC).isoformat(),
        "seed": args.seed,
        "found_total": found_total,
        "strata": {
            name: {"sampled": sum(r["stratum"] == name for r in pool), "population": sizes[name]}
            for name, _, _ in STRATA
        },  # fmt: skip
        "fit_bar_score": 80,
        "note": "label: yes = would apply / want to see near the top; no = skip",
    }
    snap.write_text(json.dumps({"meta": meta, "profile": profile, "marks": marks,
                                "jobs": pool}, indent=1, default=str))  # fmt: skip
    os.chmod(snap, 0o600)
    print(f"wrote {len(pool)} jobs: {sheet}")
    print(f"snapshot: {snap}")
    print(json.dumps(meta["strata"]))


if __name__ == "__main__":
    main()
