"""People the owner already knows, from LinkedIn's own "Connections" export (Settings > Data
privacy > Get a copy of your data). Read from the owner's upload only, never fetched from
LinkedIn, never sent to the AI; matched to job companies to show warm paths."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any

from jobagent.db import bump_version
from jobagent.dedupe import normalize_company

HEADER = ("First Name", "Last Name")
MAX_ROWS = 30_000


class NetworkFileError(Exception):
    """The upload is not a LinkedIn Connections.csv."""


def parse_connections(raw: bytes) -> list[dict[str, str]]:
    """LinkedIn puts a few "Notes:" lines above the header row, so the header is searched for."""
    text = raw.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith(HEADER[0])), None)
    if start is None or HEADER[1] not in lines[start]:
        raise NetworkFileError("this is not LinkedIn's Connections.csv (no First Name column)")
    rows = []
    for rec in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        name = " ".join(f"{rec.get(k) or ''}".strip() for k in HEADER).strip()
        company = (rec.get("Company") or "").strip()
        if name and company:
            rows.append({
                "name": name[:120], "company": company[:200],
                "position": (rec.get("Position") or "").strip()[:200],
                "url": (rec.get("URL") or "").strip()[:300],
                "email": (rec.get("Email Address") or "").strip().lower()[:200],
            })  # fmt: skip
        if len(rows) >= MAX_ROWS:
            break
    return rows


class Network:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def replace(self, rows: list[dict[str, str]]) -> int:
        """A new export replaces the last one; people added by hand stay."""
        now = datetime.now(UTC).isoformat()
        with self.conn:
            self.conn.execute("DELETE FROM connections WHERE source='linkedin'")
            self.conn.executemany(
                "INSERT INTO connections(name, company, norm_company, position, url, email,"
                " source, added_at) VALUES (?,?,?,?,?,?,'linkedin',?)",
                [(r["name"], r["company"], normalize_company(r["company"]), r["position"],
                  r["url"], r["email"], now) for r in rows],
            )  # fmt: skip
            bump_version(self.conn)
        return len(rows)

    def add(self, *, name: str, company: str, position: str, url: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO connections(name, company, norm_company, position, url, email,"
                " source, added_at) VALUES (?,?,?,?,?,'','manual',?)",
                (name, company, normalize_company(company), position, url,
                 datetime.now(UTC).isoformat()),
            )  # fmt: skip
            bump_version(self.conn)

    def clear(self) -> int:
        with self.conn:
            cur = self.conn.execute("DELETE FROM connections")
            bump_version(self.conn)
        return int(max(cur.rowcount, 0))

    def summary(self) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT norm_company) AS companies, MAX(added_at) AS at"
            " FROM connections"
        ).fetchone()
        return {"people": int(row["n"]), "companies": int(row["companies"]), "updated": row["at"]}

    def at(self, company: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, name, position, url, email, source FROM connections"
            " WHERE norm_company=? ORDER BY name",
            (normalize_company(company),),
        ).fetchall()
        return [dict(r) for r in rows]

    def matches(self, limit: int = 50) -> list[dict[str, Any]]:
        """Companies with open or applied jobs where the owner knows someone, most people first."""
        rows = self._job_rows()
        companies: dict[str, dict[str, Any]] = {}
        for r in rows:
            entry = companies.setdefault(r["norm_company"], {"company": r["company"], "jobs": []})
            if len(entry["jobs"]) < 5:
                entry["jobs"].append({"id": r["id"], "title": r["title"], "status": r["status"]})
        out = []
        for norm, entry in companies.items():
            people = self.conn.execute(
                "SELECT name, position, url FROM connections WHERE norm_company=? ORDER BY name",
                (norm,),
            ).fetchall()
            out.append({**entry, "people": [dict(p) for p in people]})
        out.sort(key=lambda e: -len(e["people"]))
        return out[:limit]

    def _job_rows(self) -> list[dict[str, Any]]:
        """Normalise job companies in Python: the name rules live in dedupe, not in SQL."""
        known = {r[0] for r in self.conn.execute("SELECT DISTINCT norm_company FROM connections")}
        if not known:
            return []
        rows = self.conn.execute(
            "SELECT id, title, company, status FROM jobs"
            " WHERE status IN ('found', 'applied', 'interview') ORDER BY id DESC"
        ).fetchall()
        out = []
        for r in rows:
            norm = normalize_company(r["company"])
            if norm in known:
                out.append({**dict(r), "norm_company": norm})
        return out
