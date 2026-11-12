"""The MCP key: a revocable token for the job-jarvis MCP server, so the password never
leaves the owner. It opens only the routes the MCP tools call (MCP_ROUTES)."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Any

from fastapi import FastAPI, Request

from jobagent.db import bump_version

FLAG = "mcp_token_sha256"
# (method, path) pairs the key may call; never login, keys, profile, outreach or the switch.
MCP_ROUTES = tuple(
    (method, re.compile(path))
    for method, path in (
        ("GET", r"/api/jobs"), ("GET", r"/api/jobs/\d+"), ("POST", r"/api/jobs/\d+/mark"),
        ("GET", r"/api/tracker"), ("PATCH", r"/api/tracker/\d+"), ("GET", r"/api/funding"),
        ("GET", r"/api/prep/\d+"), ("POST", r"/api/prep/\d+"), ("POST", r"/api/coach"),
        ("GET", r"/api/coach/\d+"), ("POST", r"/api/coach/\d+/resume"),
        ("GET", r"/api/tasks/[0-9a-f]+"), ("GET", r"/api/discover"), ("POST", r"/api/discover"),
    )
)  # fmt: skip


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def allowed(request: Request) -> bool:
    path = request.url.path
    return any(m == request.method and p.fullmatch(path) for m, p in MCP_ROUTES)


def key_valid(conn: Any, given: str) -> bool:
    row = conn.execute("SELECT value FROM flags WHERE name=?", (FLAG,)).fetchone()
    return bool(given and row and hmac.compare_digest(_digest(given), str(row["value"])))


def add_mcp_key_routes(app: FastAPI, *, owner: Any) -> None:
    def borrow() -> Any:
        return app.state.jobs_db.borrow()

    @app.get("/api/mcp/key", dependencies=[owner])
    def key_state() -> dict[str, bool]:
        with borrow() as db:
            row = db.conn.execute("SELECT 1 FROM flags WHERE name=?", (FLAG,)).fetchone()
        return {"on": row is not None}

    @app.post("/api/mcp/key", dependencies=[owner])
    def new_key() -> dict[str, str]:
        """Shown once; a new key cancels the old one."""
        token = f"jj_{secrets.token_urlsafe(32)}"
        with borrow() as db, db.conn:
            db.conn.execute(
                "INSERT INTO flags(name, value) VALUES (?, ?) ON CONFLICT(name)"
                " DO UPDATE SET value=excluded.value",
                (FLAG, _digest(token)),
            )
            bump_version(db.conn)
        return {"key": token}

    @app.delete("/api/mcp/key", dependencies=[owner])
    def drop_key() -> dict[str, bool]:
        with borrow() as db, db.conn:
            db.conn.execute("DELETE FROM flags WHERE name=?", (FLAG,))
            bump_version(db.conn)
        return {"ok": True}
