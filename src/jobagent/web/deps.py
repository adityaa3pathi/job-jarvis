"""Request-scoped database handles, shared by the route modules."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request

from jobagent.db import Database
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
