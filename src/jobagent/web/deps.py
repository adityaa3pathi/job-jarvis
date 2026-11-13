"""Request-scoped database handles, shared by the route modules."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from jobagent.db import Database
from jobagent.outreach.store import OutreachStore
from jobagent.web.store import Store


def _jobs_db(request: Request) -> Iterator[Database]:
    with request.app.state.jobs_db.borrow() as db:
        yield db


def _store(request: Request) -> Iterator[Store]:
    with request.app.state.stores.borrow() as st:
        yield st


# Module level: with postponed annotations FastAPI resolves these names from module globals.
DB = Annotated[Database, Depends(_jobs_db)]
ST = Annotated[Store, Depends(_store)]


def _extension(request: Request, db: DB) -> None:
    """The browser extension holds its own token instead of the login cookie."""
    given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    want = OutreachStore(db.conn).extension_token_hash()
    digest = hashlib.sha256(given.encode()).hexdigest()
    if not given or not want or not hmac.compare_digest(digest, want):
        raise HTTPException(401, "extension token missing or wrong; make a new one in Outreach")


Ext = Depends(_extension)
