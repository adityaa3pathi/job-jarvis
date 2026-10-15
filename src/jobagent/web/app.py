"""FastAPI routes. Every route except login and health needs the owner's session cookie."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Hashable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from jobagent import __version__
from jobagent import db as jobs_module
from jobagent.config import REPO_ROOT, Settings, ensure_data_dir
from jobagent.db import Database
from jobagent.digest import Sender, digest_last_run, sender_from_env
from jobagent.discovery import discover_all
from jobagent.discovery.base import DiscoveryResult, Fetcher, load_companies, load_searches
from jobagent.discovery.fetch import HttpFetcher, Resolver, system_resolver
from jobagent.discovery.manual import fetch_posting
from jobagent.index import Embedder, shared_embedder
from jobagent.llm import LLM, LLMError, Usage, short_cause, usage_sink
from jobagent.lock import LockHeldError, RunLock
from jobagent.models import JobFilters, ListStatus
from jobagent.policy import Policy, clear_kill_switch
from jobagent.redact import request_id
from jobagent.resume.coach import (
    MAX_JD_CHARS,
    ChatTools,
    CoachReport,
    Message,
    build_report,
    profile_digest,
    reply,
    with_facts,
)
from jobagent.resume.evidence import cards_from_profile
from jobagent.resume.jd_agent import analyze_jd
from jobagent.resume.latex import LatexError
from jobagent.resume.match import match_profile
from jobagent.resume.pipeline import run_tailor
from jobagent.runs import (
    RunTally,
    digest_after_run,
    note_policy,
    prune_after_run,
    rank_after_run,
    rules_swept,
    save_portal,
    sweep_context,
)
from jobagent.web.auth import COOKIE, SESSION_TTL_S, make_token, token_valid, verify_password
from jobagent.web.store import Store, profile_from_json, profile_to_json
from jobagent.web.uploads import MAX_SUMMARY_CHARS, MAX_UPLOAD_BYTES, UploadError, doc_to_text

MAX_LOGIN_FAILURES = 5
LOGIN_WINDOW_S = 900
MAX_GLOBAL_FAILURES = 30  # across all keys; past it every check is slowed, never refused
GLOBAL_WINDOW_S = 3600
GLOBAL_DELAY_S = 2.0  # one slowed check at a time: about 1800 guesses an hour at most
GLOBAL_WAIT_S = 30.0
PROXY_HEADER = "x-jobagent-proxy"  # set by the web app's middleware; proves Vercel sent it
LOGIN_CHECKS = 2  # scrypt needs 16 MB each; a burst must not exhaust 512 MB
LOGIN_WAIT_S = 3.0
HEALTH_TIMEOUT_S = 3.0
SHUTDOWN_WAIT_S = 10.0
SHUTDOWN_ERROR = "server shutting down"
REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
PDF_ENV = ("PATH", "HOME", "TMPDIR", "LANG")  # Tectonic must never see the app's secrets
RUN_FIELDS = ("id", "status", "started_at", "finished_at", "error")
PDF_TIMEOUT_S = 120
MAX_TASKS = 50
TASK_DEADLINE_S = 600.0  # free-tier retries can stall an AI step for tens of minutes
MAX_PDFS = 8
UPLOAD_PATHS = ("/api/profile/summary-file", "/api/profile/tex")
MULTIPART_SLACK = 64_000
PING_EVERY_S = 300  # Render spins a free service down after 15 idle minutes
REVALIDATE = "private, no-cache"  # owner data: never in a shared cache, always re-checked
GZIP_MIN_BYTES = 1000
COUNTS_TTL_S = 600.0
COUNTS_MAX = 128
GITHUB_API = "https://api.github.com"
DISCOVER_WORKFLOW = "discover.yml"
DISPATCH_WAIT_S = 600.0  # a queued workflow writes its runs row within minutes, or never
CLOCK_SKEW_S = 60.0  # the runner's clock against ours
log = logging.getLogger(__name__)
OwnerMark = Literal["found", "applied", "dropped", "interview", "rejected"]


@dataclass
class WebConfig:
    settings: Settings
    owner_hash: str
    session_secret: str
    thinker: Callable[[], LLM]
    writer: Callable[[], LLM]
    fetcher: Fetcher = field(default_factory=HttpFetcher)
    resolver: Resolver = system_resolver
    cookie_secure: bool = True
    cron_token: str = ""
    ping: Callable[[str], object] = field(default=lambda url: httpx.get(url, timeout=30))
    ping_every_s: float = PING_EVERY_S
    proxy_secret: str = ""
    task_deadline_s: float = TASK_DEADLINE_S
    shutdown_wait_s: float = SHUTDOWN_WAIT_S
    embedder: Callable[[], Embedder | None] = shared_embedder  # loaded on the first run
    digest_sender: Callable[[], Sender | None] = field(default=lambda: sender_from_env(os.environ))
    # Both set: "Find new jobs" starts the GitHub workflow instead of running here.
    github_token: str = ""
    github_repo: str = ""
    github_transport: httpx.BaseTransport | None = None


def dispatch_discover(token: str, repo: str, transport: httpx.BaseTransport | None) -> None:
    """Start the daily-discover workflow on main; GitHub answers 204 when it is queued."""
    url = f"{GITHUB_API}/repos/{repo}/actions/workflows/{DISCOVER_WORKFLOW}/dispatches"
    headers = {
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }  # fmt: skip
    with httpx.Client(transport=transport, timeout=15) as client:
        res = client.post(url, json={"ref": "main"}, headers=headers)
    if res.status_code != 204:
        raise RuntimeError(f"GitHub answered {res.status_code}")


def keep_awake(
    stop: threading.Event, ping: Callable[[str], object], every_s: float = PING_EVERY_S
) -> threading.Thread | None:
    """Ping our public URL until `stop` is set (KTD4). Only a request through Render's proxy
    counts as activity, so a localhost ping would not do; skipped when the URL is unset."""
    base = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not base:
        return None
    url = f"{base}/api/health/live"  # no DB query every 5 min

    def loop() -> None:
        while not stop.wait(every_s):
            try:
                ping(url)
            except Exception as exc:  # a missed ping is not worth failing the run
                log.warning("self-ping failed: %s", exc)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


@dataclass
class _Task:
    status: str = "running"  # running | done | failed
    result: dict[str, Any] | None = None
    error: str = ""
    code: int = 0


class _Abandoned(Exception):
    """The task passed its deadline; the owner was told to retry, so it must not write."""


def _still_wanted(cancelled: threading.Event) -> None:
    if cancelled.is_set():
        raise _Abandoned


@dataclass
class _DiscoverState:
    running: bool = False
    guard: threading.Lock = field(default_factory=threading.Lock)
    dispatched_at: float | None = None  # when we last started the GitHub run

    def awaiting_runner(self, runs: dict[str, dict[str, Any] | None]) -> bool:
        """A dispatched run counts as running until the runner's runs row appears."""
        at = self.dispatched_at
        if at is None or time.time() - at >= DISPATCH_WAIT_S:
            return False
        rows = [r for r in runs.values() if r]
        return not any(r["started_at"] >= at - CLOCK_SKEW_S for r in rows)


class Login(BaseModel):
    password: str = Field(max_length=200)


class JobsQuery(JobFilters):
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class Mark(BaseModel):
    status: OwnerMark


class SummaryIn(BaseModel):
    summary: str = Field(max_length=MAX_SUMMARY_CHARS)


class CoachIn(BaseModel):
    url: str = Field(default="", max_length=2000)
    jd_text: str = Field(default="", max_length=MAX_JD_CHARS)
    title: str = Field(default="", max_length=200)
    company: str = Field(default="", max_length=200)
    trust_host: bool = False


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class FactIn(BaseModel):
    confirmed: bool


class _Closable(Protocol):
    def close(self) -> None: ...


class _Handles[T: _Closable]:
    """Open DB handles reused across requests, each lent to one borrower at a time.
    One locked shared handle would stall pages behind a discovery write, and a libsql
    connection must never serve two threads at once."""

    def __init__(self, make: Callable[[], T], first: T, keep: int = 4) -> None:
        self._make, self._keep, self._idle = make, keep, [first]
        self._lock = threading.Lock()

    @contextmanager
    def borrow(self) -> Iterator[T]:
        with self._lock:
            handle = self._idle.pop() if self._idle else None
        handle = handle or self._make()
        try:
            yield handle
        except BaseException:
            handle.close()  # it may be broken or mid-transaction; never lend it again
            raise
        with self._lock:
            if len(self._idle) < self._keep:
                self._idle.append(handle)
                return
        handle.close()


class _TTLCache:
    """A few recent facet and count results. Keys carry the data version, so a write
    makes old entries unreachable; the TTL and size cap only bound memory."""

    def __init__(self, ttl_s: float = COUNTS_TTL_S, size: int = COUNTS_MAX) -> None:
        self._ttl, self._size = ttl_s, size
        self._items: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Hashable, make: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._lock:
            hit = self._items.get(key)
            if hit and now - hit[0] < self._ttl:
                return hit[1]
        value = make()
        with self._lock:
            self._items[key] = (now, value)
            self._items.move_to_end(key)
            while len(self._items) > self._size:
                self._items.popitem(last=False)
        return value


def _etag(request: Request, version: int) -> str:
    # The IST date is in the tag: "posted within N days" shifts at midnight with no write.
    query = hashlib.sha256(f"{request.url.path}?{request.url.query}".encode()).hexdigest()
    return f'W/"{version}-{jobs_module.today_ist().isoformat()}-{query[:16]}"'


def _not_modified(request: Request, response: Response, version: int) -> Response | None:
    """Tag the reply; a 304 when the browser already holds this version. Read the version
    before the data, so a write in between can only make the tag older, never newer."""
    tag = _etag(request, version)
    response.headers.update({"ETag": tag, "Cache-Control": REVALIDATE})
    held = {t.strip() for t in request.headers.get("if-none-match", "").split(",")}
    if tag in held:
        return Response(status_code=304, headers={"ETag": tag, "Cache-Control": REVALIDATE})
    return None


def login_key(request: Request, proxy_secret: str = "") -> tuple[str, str]:
    """Who is logging in, and which header said so. Browsers reach us through Vercel, whose
    edge writes the client address; in X-Forwarded-For the right-most hop is Vercel itself.
    With a proxy secret set, those headers count only on requests that carry it."""
    sent = request.headers.get(PROXY_HEADER, "")
    if proxy_secret and not hmac.compare_digest(sent.encode(), proxy_secret.encode()):
        return (request.client.host if request.client else "?"), "client"
    vercel = request.headers.get("x-vercel-forwarded-for", "").split(",")[0].strip()
    if vercel:
        return vercel, "x-vercel-forwarded-for"
    hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",")]
    hops = [h for h in hops if h]
    if len(hops) >= 2:
        return hops[-2], "x-forwarded-for"
    return (request.client.host if request.client else "?"), "client"


def mask_ip(ip: str) -> str:
    if ip.count(".") == 3:
        return ip.rsplit(".", 1)[0] + ".x"
    if ":" in ip:
        return ":".join(ip.split(":")[:3]) + ":x"
    return "?"


def db_ping(db: Database) -> None:
    db.conn.execute("SELECT 1").fetchone()


class _DbProbe:
    """A hung Turso call must not hang the health check, so it runs on its own thread; while
    one still runs, later checks wait on it instead of stacking threads and connections."""

    def __init__(self, handles: _Handles[Database]) -> None:
        self.handles, self.lock = handles, threading.Lock()
        self.thread: threading.Thread | None = None
        self.up: list[bool] = []

    def _check(self, up: list[bool]) -> None:
        try:
            with self.handles.borrow() as db:
                db_ping(db)
            up.append(True)
        except Exception as exc:
            log.warning("health: database check failed: %s", short_cause(exc))

    def __call__(self, timeout_s: float) -> bool:
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.up = []
                self.thread = threading.Thread(target=self._check, args=(self.up,), daemon=True)
                self.thread.start()
            thread, up = self.thread, self.up
        thread.join(timeout_s)
        return bool(up)


def _jobs_db(request: Request) -> Iterator[Database]:
    handles: _Handles[Database] = request.app.state.jobs_db
    with handles.borrow() as db:
        yield db


def _store(request: Request) -> Iterator[Store]:
    handles: _Handles[Store] = request.app.state.stores
    with handles.borrow() as st:
        yield st


# Module level: with postponed annotations FastAPI resolves these names from module globals.
DB = Annotated[Database, Depends(_jobs_db)]
ST = Annotated[Store, Depends(_store)]


def create_app(cfg: WebConfig) -> FastAPI:
    if not cfg.owner_hash or not cfg.session_secret:
        raise RuntimeError("JOBAGENT_OWNER_HASH and JOBAGENT_SESSION_SECRET must be set")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await run_in_threadpool(shutdown)

    app = FastAPI(
        title="job-agent", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MIN_BYTES)
    app.state.settings = cfg.settings
    settings = cfg.settings
    ensure_data_dir(settings)
    # Schema setup runs once here; later handles skip it.
    jobs_db = _Handles(
        lambda: Database(settings.db_path, settings.max_years_asked, schema=False),
        Database(settings.db_path, settings.max_years_asked),
    )
    stores = _Handles(lambda: Store(settings.db_path, schema=False), Store(settings.db_path))
    app.state.jobs_db, app.state.stores = jobs_db, stores
    with jobs_db.borrow() as db:
        for name in db.ensure_indexes():
            log.warning("index %s was missing; created it", name)
    counts = _TTLCache()
    failures: dict[str, list[float]] = {}
    all_failures: list[float] = []
    failures_lock = threading.Lock()
    checks = threading.BoundedSemaphore(LOGIN_CHECKS)
    slowed = threading.BoundedSemaphore(1)
    db_up = _DbProbe(jobs_db)
    if not cfg.proxy_secret:
        log.warning("JOBAGENT_PROXY_SECRET is not set: forwarded client headers are taken as sent")
    key_logged = threading.Event()
    discover_state = _DiscoverState()
    closing = threading.Event()

    def db_stopped() -> bool:
        with stores.borrow() as st:
            return st.kill_switch()

    def load_policy() -> Policy:
        return Policy.load(REPO_ROOT / "policy.yaml", settings.stop_file, stop_check=db_stopped)

    def owner(request: Request) -> None:
        token = request.cookies.get(COOKIE, "")
        if not token or not token_valid(cfg.session_secret, token):
            raise HTTPException(401, "login needed")

    Owner = Depends(owner)

    @app.get("/api/health/live")
    def live() -> dict[str, bool]:
        return {"ok": True}  # never touches the DB: cheap enough for frequent pings

    @app.get("/api/health")
    def health() -> JSONResponse:
        up = db_up(HEALTH_TIMEOUT_S)
        body = {"ok": up, "db": "ok" if up else "down", "version": __version__}
        return JSONResponse(body, status_code=200 if up else 503)

    @app.middleware("http")
    async def upload_limit(request: Request, call_next: Any) -> Any:
        # Refuse by the declared size before the multipart body is read into memory.
        size = request.headers.get("content-length", "")
        too_big = size.isdigit() and int(size) > MAX_UPLOAD_BYTES + MULTIPART_SLACK
        if request.url.path in UPLOAD_PATHS and too_big:
            return JSONResponse({"detail": "file is larger than 5 MB"}, status_code=413)
        return await call_next(request)

    @app.middleware("http")
    async def request_ids(request: Request, call_next: Any) -> Any:
        # Added last, so it runs first: even refused uploads get an id.
        given = request.headers.get("x-request-id", "")
        rid = given if REQUEST_ID.fullmatch(given) else secrets.token_hex(8)
        token = request_id.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id.reset(token)
        response.headers["X-Request-ID"] = rid
        return response

    def _forget(who: str, at: float) -> None:
        with failures_lock:
            for seen in (failures.get(who, []), all_failures):
                if at in seen:
                    seen.remove(at)

    def _check_password(password: str, who: str, at: float) -> bool:
        if not checks.acquire(timeout=LOGIN_WAIT_S):
            _forget(who, at)
            raise HTTPException(429, "login is busy, try again in a moment")
        try:
            return verify_password(password, cfg.owner_hash)
        finally:
            checks.release()

    @app.post("/api/login")
    def login(body: Login, request: Request, response: Response) -> dict[str, bool]:
        # Per client, so strangers cannot lock the owner out (KTD8).
        now, (who, source) = time.time(), login_key(request, cfg.proxy_secret)
        if not key_logged.is_set():
            key_logged.set()
            log.info("login key comes from %s: %s", source, mask_ip(who))
        with failures_lock:
            for key in list(failures):
                failures[key] = [t for t in failures[key] if now - t < LOGIN_WINDOW_S]
                if not failures[key]:
                    del failures[key]
            all_failures[:] = [t for t in all_failures if now - t < GLOBAL_WINDOW_S]
            if len(failures.get(who, [])) >= MAX_LOGIN_FAILURES:
                raise HTTPException(429, "too many attempts, wait 15 minutes")
            slow = len(all_failures) >= MAX_GLOBAL_FAILURES
            # Counted before the check, so a burst cannot all slip past the limit.
            failures.setdefault(who, []).append(now)
            all_failures.append(now)
        # Past the global ceiling a correct password still works, only slower.
        if slow and not slowed.acquire(timeout=GLOBAL_WAIT_S):
            _forget(who, now)
            raise HTTPException(429, "login is busy, try again in a moment")
        try:
            if slow:
                time.sleep(GLOBAL_DELAY_S)
            ok = _check_password(body.password, who, now)
        finally:
            if slow:
                slowed.release()
        if not ok:
            raise HTTPException(401, "wrong password")
        _forget(who, now)
        response.set_cookie(
            COOKIE, make_token(cfg.session_secret), max_age=SESSION_TTL_S,
            httponly=True, secure=cfg.cookie_secure, samesite="lax",
        )  # fmt: skip
        return {"ok": True}

    @app.post("/api/logout")
    def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/api/me", dependencies=[Owner])
    def me(st: ST) -> dict[str, Any]:
        return {"ok": True, "kill_switch": st.kill_switch() or settings.stop_file.exists()}

    # --- jobs and discovery ---

    @app.get("/api/jobs", dependencies=[Owner])
    def jobs(
        db: DB, query: Annotated[JobsQuery, Query()], request: Request, response: Response
    ) -> Any:
        version = db.data_version()
        if hit := _not_modified(request, response, version):
            return hit
        rows, total = db.query_jobs(query, limit=query.limit, offset=query.offset)
        # Counts ignore the page, the sort and the source tab, so one entry serves them all.
        filters = query.model_dump(exclude={"source", "sort", "limit", "offset"})
        key = ("sources", version, jobs_module.today_ist(), *sorted(filters.items()))
        sources = counts.get(key, lambda: db.source_counts(query))
        return {"items": [dict(r) for r in rows], "total": total, "sources": sources}

    @app.get("/api/jobs/facets", dependencies=[Owner])
    def job_facets(
        db: DB, request: Request, response: Response, status: ListStatus = "found"
    ) -> Any:
        version = db.data_version()
        if hit := _not_modified(request, response, version):
            return hit
        return counts.get(("facets", version, status), lambda: db.facets(status))

    @app.post("/api/jobs/{job_id}/mark", dependencies=[Owner])
    def mark(job_id: int, body: Mark, db: DB) -> dict[str, bool]:
        if db.get_job(job_id) is None:
            raise HTTPException(404, "no such job")
        db.mark(job_id, body.status)  # also the ranking's training label (U14)
        return {"ok": True}

    def _discover() -> None:
        lock, tally, stop_ping = RunLock(settings.lock_file), RunTally(), threading.Event()
        held, run_id = False, None
        try:
            with stores.borrow() as st:
                try:
                    lock.acquire()
                    held = True
                    run_id = this_run = st.start_run()
                    keep_awake(stop_ping, cfg.ping, cfg.ping_every_s)
                    with jobs_db.borrow() as db:

                        def save(part: DiscoveryResult) -> None:
                            save_portal(
                                part, db=db, st=st, run_id=this_run, tally=tally,
                                after_runs=settings.close_after_clean_runs,
                            )  # fmt: skip

                        policy = load_policy()
                        discover_all(
                            load_companies(REPO_ROOT / "companies.yaml"), cfg.fetcher,
                            policy, load_searches(REPO_ROOT / "searches.yaml"),
                            on_portal_done=save,
                            ctx=sweep_context(db, settings.detail_fetch_cap),
                        )  # fmt: skip
                        note_policy(policy, tally)
                        rules_swept(db, tally)
                        prune_after_run(db, tally)
                        rank_after_run(
                            db, st, tally, embedder=cfg.embedder(), llm=cfg.thinker(),
                            settings=settings,
                        )  # fmt: skip
                    st.finish_run(run_id, "done", "", tally.as_dict())
                    with jobs_db.borrow() as db:
                        digest_after_run(db, run_id, cfg.digest_sender(), settings)
                except Exception as exc:  # record every failure; saved portals stay saved
                    held_elsewhere = isinstance(exc, LockHeldError)
                    error = str(exc) if held_elsewhere else f"{type(exc).__name__}: {exc}"
                    if not held_elsewhere:
                        log.exception("discovery run failed")
                    if run_id is None:
                        st.failed_run(error)
                    else:
                        st.finish_run(run_id, "failed", error, tally.as_dict())
        except Exception:  # the run state could not be written; "running" must still end
            log.exception("could not record the discovery run")
        finally:
            stop_ping.set()
            try:
                if held:
                    lock.release()
            finally:
                discover_state.running = False

    on_github = bool(cfg.github_token and cfg.github_repo)

    def discover_view() -> dict[str, Any]:
        with stores.borrow() as st:
            runs = st.runs()
        # A "running" row counts only while another live process holds the lock;
        # after a restart the row is a leftover and must not block a new run.
        current = runs["current"]
        elsewhere = current is not None and RunLock(settings.lock_file).held_elsewhere()
        if on_github:  # the runner holds its own lock; its fresh "running" row is the signal
            elsewhere = current is not None or discover_state.awaiting_runner(runs)
        running = discover_state.running or elsewhere
        return {
            "running": running,
            "last": runs["last"] or {},
            "current": current if running else None,
        }

    def start_here() -> dict[str, Any]:
        with discover_state.guard:
            started = not discover_state.running
            discover_state.running = True
        if started:
            threading.Thread(target=_discover, daemon=True).start()
        return {"started": started, **discover_view()}

    def start_on_github() -> dict[str, Any]:
        """Render's small CPU cannot run discovery and answer its health check at once."""
        with discover_state.guard:
            if (view := discover_view())["running"]:
                return {"started": False, **view, "via": "github"}
            try:
                dispatch_discover(cfg.github_token, cfg.github_repo, cfg.github_transport)
            except Exception as exc:
                log.exception("starting the GitHub discovery run failed")
                raise HTTPException(502, f"could not start the GitHub run: {exc}") from exc
            discover_state.dispatched_at = time.time()
        return {"started": True, **discover_view(), "via": "github"}

    @app.post("/api/discover", dependencies=[Owner])
    def start_discover() -> dict[str, Any]:
        # Same shape as GET /api/discover, which the page reads.
        return start_on_github() if on_github else start_here()

    def cron(request: Request) -> None:
        """A scheduler holds a token instead of a login."""
        given = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not cfg.cron_token or not hmac.compare_digest(given, cfg.cron_token):
            raise HTTPException(401, "bad cron token")

    @app.post("/api/cron/discover", dependencies=[Depends(cron)])
    def cron_discover() -> dict[str, Any]:
        # The fallback: always runs in this process. The daily run now lives on GitHub.
        return start_here()

    @app.post("/api/cron/digest", dependencies=[Depends(cron)])
    def cron_digest(db: DB) -> dict[str, Any]:
        """Send the last finished run's digest; never twice for one run (U15)."""
        return digest_last_run(db, cfg.digest_sender(), limit=settings.ranking.digest_top_n)

    @app.get("/api/cron/discover", dependencies=[Depends(cron)])
    def cron_discover_status(response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return discover_view()

    @app.get("/api/discover", dependencies=[Owner])
    def discover_status(response: Response) -> dict[str, Any]:
        # Run state lives in memory and moves with the clock: no version can tag it.
        response.headers["Cache-Control"] = "no-store"
        return discover_view()

    @app.get("/api/searches", dependencies=[Owner])
    def searches() -> list[dict[str, str]]:
        return [s.model_dump() for s in load_searches(REPO_ROOT / "searches.yaml")]

    @app.post("/api/kill-switch/{state}", dependencies=[Owner])
    def kill_switch(state: Literal["on", "off"], st: ST) -> dict[str, bool]:
        st.set_kill_switch(state == "on")
        if state == "off":
            clear_kill_switch(settings.stop_file)  # the local trigger too, or "off" would not stick
        return {"kill_switch": state == "on"}

    # --- profile ---

    @app.get("/api/profile", dependencies=[Owner])
    def get_profile(st: ST, request: Request, response: Response) -> Any:
        if hit := _not_modified(request, response, st.data_version()):
            return hit
        p = st.profile()
        return {
            "summary": p["summary"],
            "has_tex": bool(p["base_tex"]),
            "updated_at": p["updated_at"],
        }

    @app.put("/api/profile/summary", dependencies=[Owner])
    def put_summary(body: SummaryIn, st: ST) -> dict[str, int]:
        st.save_profile(summary=body.summary)
        return {"chars": len(body.summary)}

    def _read(file: UploadFile) -> bytes:
        # Sync routes: parsing runs in the threadpool, not on the event loop.
        # One byte over the cap is enough for doc_to_text to refuse it.
        return file.file.read(MAX_UPLOAD_BYTES + 1)

    @app.post("/api/profile/summary-file", dependencies=[Owner])
    def upload_summary(st: ST, file: Annotated[UploadFile, File()]) -> dict[str, Any]:
        try:
            text = doc_to_text(file.filename or "", _read(file))
        except UploadError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not text:
            raise HTTPException(400, "no text found in the file")
        if len(text) > MAX_SUMMARY_CHARS:
            raise HTTPException(
                422, f"the summary has {len(text):,} characters; the limit is "
                f"{MAX_SUMMARY_CHARS:,}. Shorten it and upload again.",
            )  # fmt: skip
        st.save_profile(summary=text)
        return {"chars": len(text), "summary": text}

    @app.post("/api/profile/tex", dependencies=[Owner])
    def upload_tex(st: ST, file: Annotated[UploadFile, File()]) -> dict[str, int]:
        if not (file.filename or "").lower().endswith(".tex"):
            raise HTTPException(400, "upload a .tex file")
        try:
            text = doc_to_text(file.filename or "", _read(file))
        except UploadError as exc:
            raise HTTPException(400, str(exc)) from exc
        st.save_profile(base_tex=text)
        return {"chars": len(text)}

    # --- background tasks: AI steps outlast proxy timeouts, so the page polls instead ---

    tasks: dict[str, _Task] = {}
    tasks_lock = threading.Lock()
    pool = ThreadPoolExecutor(max_workers=2)

    def finish(task: _Task, code: int, error: str, result: dict[str, Any] | None = None) -> None:
        # Status last and under the lock, so a poll never sees "failed" without its error.
        with tasks_lock:
            if task.status == "running":
                task.result, task.code, task.error = result, code, error
                task.status = "failed" if error else "done"

    def submit(work: Callable[[Store, threading.Event], dict[str, Any]]) -> dict[str, str]:
        if closing.is_set():
            raise HTTPException(503, "the server is shutting down; try again in a minute")
        task_id, task, cancelled = secrets.token_hex(8), _Task(), threading.Event()
        rid = request_id.get()
        with tasks_lock:
            finished = [k for k, v in tasks.items() if v.status != "running"]
            for key in finished[: max(0, len(tasks) - MAX_TASKS)]:
                del tasks[key]
            tasks[task_id] = task

        def body() -> None:
            request_id.set(rid)  # a new thread starts with an empty context
            used = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "tokens_cached": 0, "ms": 0}

            def record(usage: Usage, st: Store) -> None:
                used["calls"] += 1
                for name in ("tokens_in", "tokens_out", "tokens_cached", "ms"):
                    used[name] += getattr(usage, name)
                st.add_usage(usage)

            try:
                with stores.borrow() as st, usage_sink(lambda u: record(u, st)):
                    finish(task, 0, "", work(st, cancelled))
            except _Abandoned:
                log.warning("background task %s ended after its deadline; nothing saved", task_id)
            except HTTPException as exc:
                finish(task, exc.status_code, str(exc.detail) or "the task failed")
            except Exception as exc:  # report, never crash the worker
                log.exception("background task %s failed", task_id)
                finish(task, 500, f"{type(exc).__name__}: {exc}"[:500])
            finally:
                summary = " ".join(f"{k}={v}" for k, v in used.items())
                log.info("task %s AI usage: %s", task_id, summary)

        def run() -> None:
            request_id.set(rid)
            # A thread cannot be killed; past the deadline we stop waiting and free the worker.
            worker = threading.Thread(target=body, daemon=True)
            worker.start()
            worker.join(cfg.task_deadline_s)
            if worker.is_alive():
                cancelled.set()
                log.error("background task %s passed its deadline", task_id)
                minutes = max(1, round(cfg.task_deadline_s / 60))
                finish(task, 504, f"took longer than {minutes} min; stopped waiting. Try again.")

        pool.submit(run)
        return {"task_id": task_id}

    def shutdown() -> None:
        """Deploys send SIGTERM: refuse new work, give running work a moment, then record
        what was cut off so the owner is not left looking at a run that never ends."""
        closing.set()
        end = time.monotonic() + cfg.shutdown_wait_s
        while time.monotonic() < end:
            with tasks_lock:
                busy = any(t.status == "running" for t in tasks.values())
            if not busy and not discover_state.running:
                break
            time.sleep(0.05)
        if discover_state.running:
            try:
                with stores.borrow() as st:
                    current = st.runs()["current"]
                    if current:
                        counts = {k: v for k, v in current.items() if k not in RUN_FIELDS}
                        st.finish_run(current["id"], "failed", SHUTDOWN_ERROR, counts)
                log.warning("discovery run marked failed: %s", SHUTDOWN_ERROR)
            except Exception:
                log.exception("could not mark the discovery run failed at shutdown")
        with tasks_lock:
            running = [t for t in tasks.values() if t.status == "running"]
        for task in running:
            finish(task, 503, f"{SHUTDOWN_ERROR}; try again in a minute")
        pool.shutdown(wait=False, cancel_futures=True)

    @app.get("/api/tasks/{task_id}", dependencies=[Owner])
    def task_status(task_id: str) -> dict[str, Any]:
        with tasks_lock:
            task = tasks.get(task_id)
            if task is None:
                raise HTTPException(404, "no such task (the server may have restarted)")
            return asdict(task)

    # --- coach ---

    def _ai_budget(st: Store) -> None:
        """Refuse a new AI task once today's budget (IST day) is used up."""
        llm, used = settings.llm, st.usage_today()
        over = [
            f"{used[kind]:,} of {limit:,} {kind}"
            for kind, limit in (
                ("tokens", llm.daily_token_budget),
                ("calls", llm.daily_call_budget),
            )
            if limit and used[kind] >= limit
        ]
        if over:
            raise HTTPException(
                429, f"Daily AI budget reached ({'; '.join(over)} used today). "
                "New AI tasks start again after midnight IST.",
            )  # fmt: skip

    def _session(st: Store, session_id: int) -> Any:
        row = st.session(session_id)
        if row is None:
            raise HTTPException(404, "no such coach session")
        return row

    def _session_view(st: Store, session_id: int) -> dict[str, Any]:
        row = _session(st, session_id)
        return {
            "id": row["id"], "title": row["title"], "company": row["company"],
            "source_url": row["source_url"], "created_at": row["created_at"],
            "report": json.loads(row["report_json"]),
            "messages": st.messages(session_id), "facts": st.facts(session_id),
            "builds": st.builds(session_id),
        }  # fmt: skip

    def _jd_from(body: CoachIn) -> tuple[str, str, str]:
        if body.jd_text.strip():
            head = "\n".join(x for x in (body.title, body.company) if x)
            return f"{head}\n\n{body.jd_text}".strip(), body.title, body.company
        if not body.url:
            raise HTTPException(400, "paste a job link or the job description text")
        host = (urlsplit(body.url).hostname or "").lower()
        policy = load_policy()
        got = fetch_posting(
            body.url, companies=load_companies(REPO_ROOT / "companies.yaml"),
            fetcher=cfg.fetcher, policy=policy, resolver=cfg.resolver,
            confirmed_hosts={host} if body.trust_host and host else None,
        )  # fmt: skip
        if got.status == "needs_owner_confirmation":
            raise HTTPException(409, f"{got.reason}. Trust this site, or paste the JD text.")
        if got.status != "ok" or got.posting is None:
            raise HTTPException(502, f"could not read the page ({got.reason}). Paste the JD text.")
        p = got.posting
        title, company = body.title or p.title, body.company or p.company
        return f"{title}\n{company}\n\n{p.description}"[:MAX_JD_CHARS], title, company

    @app.post("/api/coach", dependencies=[Owner])
    def coach(body: CoachIn, st: ST) -> dict[str, str]:
        summary = st.profile()["summary"]
        if not summary:
            raise HTTPException(400, "add your career summary on the Profile page first")
        _ai_budget(st)
        jd_text, title, company = _jd_from(body)
        facts = st.confirmed_facts()

        def work(st: Store, cancelled: threading.Event) -> dict[str, Any]:
            # Each AI step here has a rules fallback (plain JD scan, raw summary, rules report).
            try:
                thinker = cfg.thinker()
                profile = analyze_jd(jd_text, thinker, cache=st)
                digest = with_facts(profile_digest(summary, thinker, st), facts)
                coverage = match_profile(profile, cards_from_profile(summary, facts))
                report = build_report(profile, coverage, digest, thinker, cache=st)
            except LLMError as exc:
                raise HTTPException(502, f"AI step failed: {exc}") from exc
            _still_wanted(cancelled)
            session_id = st.add_session(
                url=body.url, title=title or profile.role_title, company=company,
                jd_text=jd_text, profile=profile_to_json(profile), report=report.to_json(),
            )  # fmt: skip
            return {"session_id": session_id}

        return submit(work)

    @app.get("/api/coach", dependencies=[Owner])
    def coach_list(st: ST, request: Request, response: Response) -> Any:
        if hit := _not_modified(request, response, st.data_version()):
            return hit
        return st.sessions()

    @app.get("/api/coach/{session_id}", dependencies=[Owner])
    def coach_get(session_id: int, st: ST, request: Request, response: Response) -> Any:
        if hit := _not_modified(request, response, st.data_version()):
            return hit
        return _session_view(st, session_id)

    @app.post("/api/coach/{session_id}/chat", dependencies=[Owner])
    def chat(session_id: int, body: ChatIn, st: ST) -> dict[str, str]:
        row = _session(st, session_id)
        _ai_budget(st)
        history = [Message(m["role"], m["text"]) for m in st.messages(session_id)]
        report, summary = CoachReport.from_json(row["report_json"]), st.profile()["summary"]
        job, facts = profile_from_json(row["profile_json"]), st.confirmed_facts()
        tools = ChatTools(
            facts=[*facts, *(ln.strip() for ln in summary.splitlines() if ln.strip())],
            posting=row["jd_text"],
            sessions=[s for s in st.sessions() if s["id"] != session_id],
        )

        def work(st: Store, cancelled: threading.Event) -> dict[str, Any]:
            try:
                llm = cfg.thinker()
                digest = with_facts(profile_digest(summary, llm, st), facts)
                turn = reply(report, digest, history, body.message, llm, job=job, tools=tools)
            except LLMError as exc:
                raise HTTPException(502, f"AI step failed: {exc}") from exc
            _still_wanted(cancelled)
            st.add_message(session_id, "user", body.message)
            _still_wanted(cancelled)
            st.add_message(session_id, "coach", turn.reply)
            for fact in turn.new_facts[:5]:
                _still_wanted(cancelled)
                st.add_fact(session_id, fact.strip()[:500])  # pending until the owner confirms
            return {"session_id": session_id}

        return submit(work)

    @app.post("/api/coach/{session_id}/facts/{fact_id}", dependencies=[Owner])
    def confirm_fact(session_id: int, fact_id: int, body: FactIn, st: ST) -> dict[str, bool]:
        if not st.set_fact(session_id, fact_id, body.confirmed):
            raise HTTPException(404, "no such fact")
        return {"ok": True}

    @app.get("/api/profile/facts", dependencies=[Owner])
    def profile_facts(st: ST, request: Request, response: Response) -> Any:
        if hit := _not_modified(request, response, st.data_version()):
            return hit
        return st.profile_facts()

    @app.delete("/api/profile/facts/{fact_id}", dependencies=[Owner])
    def delete_fact(fact_id: int, st: ST) -> dict[str, bool]:
        if not st.delete_fact(fact_id):
            raise HTTPException(404, "no such fact")
        return {"ok": True}

    @app.post("/api/coach/{session_id}/resume", dependencies=[Owner])
    def build_resume(session_id: int, st: ST) -> dict[str, str]:
        row = _session(st, session_id)
        prof = st.profile()
        if not prof["base_tex"]:
            raise HTTPException(400, "upload your base resume .tex on the Profile page first")
        _ai_budget(st)
        cards = cards_from_profile(prof["summary"], st.confirmed_facts())
        jd_text, profile = row["jd_text"], profile_from_json(row["profile_json"])

        def work(st: Store, cancelled: threading.Event) -> dict[str, Any]:
            try:
                outcome = run_tailor(
                    jd_text, prof["base_tex"], cards, cfg.thinker(),
                    writer_llm=cfg.writer(), profile=profile,
                )  # fmt: skip
            except (LatexError, LLMError) as exc:
                raise HTTPException(502, f"resume build failed: {exc}") from exc
            _still_wanted(cancelled)
            build_id = st.add_build(session_id, outcome.tailored.latex, outcome.report)
            return {"session_id": session_id, "build_id": build_id, "report": outcome.report}

        return submit(work)

    @app.get("/api/builds", dependencies=[Owner])
    def builds(st: ST, request: Request, response: Response) -> Any:
        if hit := _not_modified(request, response, st.data_version()):
            return hit
        return st.builds()

    def _build(st: Store, build_id: int) -> Any:
        row = st.build(build_id)
        if row is None:
            raise HTTPException(404, "no such build")
        return row

    @app.get("/api/builds/{build_id}/report", dependencies=[Owner])
    def build_report_md(build_id: int, st: ST) -> PlainTextResponse:
        return PlainTextResponse(_build(st, build_id)["report"])

    @app.get("/api/builds/{build_id}/tex", dependencies=[Owner])
    def build_tex(build_id: int, st: ST) -> Response:
        return Response(
            _build(st, build_id)["tex"], media_type="application/x-tex",
            headers={"Content-Disposition": f'attachment; filename="resume-{build_id}.tex"'},
        )  # fmt: skip

    pdfs: dict[int, bytes] = {}  # recent builds only; after a restart the PDF is built again
    pdfs_lock = threading.Lock()

    @app.post("/api/builds/{build_id}/pdf", dependencies=[Owner])
    def start_pdf(build_id: int, st: ST) -> dict[str, str]:
        """Tectonic can take two minutes, longer than a proxy waits, so the page polls."""
        tex = _build(st, build_id)["tex"]

        def work(_: Store, cancelled: threading.Event) -> dict[str, Any]:
            with pdfs_lock:
                ready = build_id in pdfs
            if not ready:
                pdf = compile_pdf(tex)
                _still_wanted(cancelled)
                with pdfs_lock:
                    pdfs[build_id] = pdf
                    for old in list(pdfs)[: max(0, len(pdfs) - MAX_PDFS)]:
                        del pdfs[old]
            return {"build_id": build_id}

        return submit(work)

    @app.get("/api/builds/{build_id}/pdf", dependencies=[Owner])
    def build_pdf(build_id: int) -> Response:
        with pdfs_lock:
            pdf = pdfs.get(build_id)
        if pdf is None:
            raise HTTPException(404, "PDF not built yet; use the PDF button")
        return Response(
            pdf, media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="resume-{build_id}.pdf"'},
        )  # fmt: skip

    return app


# Tectonic runs XeTeX; common resume templates call these pdfTeX-only commands.
XETEX_SHIM = (
    "\\providecommand{\\pdfglyphtounicode}[2]{}\n"
    "\\ifdefined\\pdfgentounicode\\else\\newcount\\pdfgentounicode\\fi\n"
)


def compile_pdf(tex: str, timeout: float = PDF_TIMEOUT_S) -> bytes:
    """Tectonic in untrusted mode: no shell escape, so resume text cannot run commands."""
    binary = shutil.which("tectonic")
    if not binary:
        raise HTTPException(503, "PDF builder (tectonic) is not installed here; download .tex")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "resume.tex"
        src.write_text(XETEX_SHIM + tex)
        env = {name: value for name in PDF_ENV if (value := os.environ.get(name))}
        try:
            done = subprocess.run(
                [binary, "--untrusted", "--outdir", tmp, str(src)],
                capture_output=True, timeout=timeout, check=False, env=env,
            )  # fmt: skip
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(504, "PDF build timed out") from exc
        pdf = Path(tmp) / "resume.pdf"
        if done.returncode != 0 or not pdf.exists():
            tail = done.stderr.decode(errors="replace")[-400:]
            raise HTTPException(422, f"LaTeX did not compile: {tail}")
        return pdf.read_bytes()


def app_from_env() -> FastAPI:
    """Entry point for uvicorn: `uvicorn --factory jobagent.web.app:app_from_env`."""
    from jobagent.config import check_config, load_settings
    from jobagent.llm import GeminiLLM
    from jobagent.redact import Redactor, configure_logging

    # Task tracebacks reach the host's logs, one JSON object per line.
    configure_logging(Redactor.from_environment(), json_lines=True)
    check_config()
    log.info("database: %s", "turso" if os.environ.get("TURSO_DATABASE_URL") else "local file")
    settings = load_settings()
    return create_app(
        WebConfig(
            settings=settings,
            owner_hash=os.environ.get("JOBAGENT_OWNER_HASH", ""),
            session_secret=os.environ.get("JOBAGENT_SESSION_SECRET", ""),
            thinker=lambda: GeminiLLM.from_settings(settings.llm, 0.0),
            writer=lambda: GeminiLLM.from_settings(settings.llm, 0.5),
            cookie_secure=os.environ.get("JOBAGENT_COOKIE_SECURE", "1") != "0",
            cron_token=os.environ.get("JOBAGENT_CRON_TOKEN", ""),
            proxy_secret=os.environ.get("JOBAGENT_PROXY_SECRET", ""),
            task_deadline_s=float(os.environ.get("JOBAGENT_TASK_DEADLINE_S", TASK_DEADLINE_S)),
            github_token=os.environ.get("GITHUB_DISPATCH_TOKEN", ""),
            github_repo=os.environ.get("GITHUB_REPO", ""),
        )
    )
