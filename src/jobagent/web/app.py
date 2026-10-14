"""FastAPI routes. Every route except login and health needs the owner's session cookie."""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from jobagent.config import REPO_ROOT, Settings, ensure_data_dir
from jobagent.db import OWNER, Database
from jobagent.discovery import discover_all
from jobagent.discovery.base import DiscoveryResult, Fetcher, load_companies, load_searches
from jobagent.discovery.fetch import HttpFetcher, Resolver, system_resolver
from jobagent.discovery.manual import fetch_posting
from jobagent.llm import LLM, LLMError
from jobagent.lock import LockHeldError, RunLock
from jobagent.policy import Policy, clear_kill_switch
from jobagent.resume.coach import MAX_JD_CHARS, CoachReport, Message, build_report, reply
from jobagent.resume.evidence import cards_from_summary
from jobagent.resume.jd_agent import analyze_jd
from jobagent.resume.latex import LatexError
from jobagent.resume.match import match_profile
from jobagent.resume.pipeline import run_tailor
from jobagent.runs import RunTally, save_portal
from jobagent.web.auth import COOKIE, SESSION_TTL_S, make_token, token_valid, verify_password
from jobagent.web.store import Store, profile_from_json, profile_to_json
from jobagent.web.uploads import MAX_UPLOAD_BYTES, UploadError, doc_to_text

MAX_LOGIN_FAILURES = 5
LOGIN_WINDOW_S = 900
PDF_TIMEOUT_S = 120
MAX_TASKS = 50
TASK_DEADLINE_S = 600.0  # free-tier retries can stall an AI step for tens of minutes
MAX_PDFS = 8
UPLOAD_PATHS = ("/api/profile/summary-file", "/api/profile/tex")
MULTIPART_SLACK = 64_000
PING_EVERY_S = 300  # Render spins a free service down after 15 idle minutes
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
    task_deadline_s: float = TASK_DEADLINE_S


def keep_awake(
    stop: threading.Event, ping: Callable[[str], object], every_s: float = PING_EVERY_S
) -> threading.Thread | None:
    """Ping our public URL until `stop` is set (KTD4). Only a request through Render's proxy
    counts as activity, so a localhost ping would not do; skipped when the URL is unset."""
    base = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not base:
        return None
    url = f"{base}/api/health"

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


class Login(BaseModel):
    password: str = Field(max_length=200)


class Mark(BaseModel):
    status: OwnerMark


class SummaryIn(BaseModel):
    summary: str = Field(max_length=100_000)


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
    app = FastAPI(title="job-agent", docs_url=None, redoc_url=None, openapi_url=None)
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
    failures: dict[str, list[float]] = {}
    failures_lock = threading.Lock()
    discover_state = _DiscoverState()

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

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"ok": "yes"}

    @app.middleware("http")
    async def upload_limit(request: Request, call_next: Any) -> Any:
        # Refuse by the declared size before the multipart body is read into memory.
        size = request.headers.get("content-length", "")
        too_big = size.isdigit() and int(size) > MAX_UPLOAD_BYTES + MULTIPART_SLACK
        if request.url.path in UPLOAD_PATHS and too_big:
            return JSONResponse({"detail": "file is larger than 5 MB"}, status_code=413)
        return await call_next(request)

    @app.post("/api/login")
    def login(body: Login, request: Request, response: Response) -> dict[str, bool]:
        # Per client, so strangers cannot lock the owner out; --proxy-headers sets the host.
        now, who = time.time(), request.client.host if request.client else "?"
        with failures_lock:
            for key in list(failures):
                failures[key] = [t for t in failures[key] if now - t < LOGIN_WINDOW_S]
                if not failures[key]:
                    del failures[key]
            if len(failures.get(who, [])) >= MAX_LOGIN_FAILURES:
                raise HTTPException(429, "too many attempts, wait 15 minutes")
        if not verify_password(body.password, cfg.owner_hash):
            with failures_lock:
                failures.setdefault(who, []).append(now)
            raise HTTPException(401, "wrong password")
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
    def jobs(db: DB, status: str | None = None, source: str | None = None) -> list[dict[str, Any]]:
        return [dict(r) for r in db.list_jobs(status=status, source=source)]

    @app.post("/api/jobs/{job_id}/mark", dependencies=[Owner])
    def mark(job_id: int, body: Mark, db: DB) -> dict[str, bool]:
        if db.get_job(job_id) is None:
            raise HTTPException(404, "no such job")
        db.set_status(job_id, body.status, OWNER)
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

                        discover_all(
                            load_companies(REPO_ROOT / "companies.yaml"), cfg.fetcher,
                            load_policy(), load_searches(REPO_ROOT / "searches.yaml"),
                            on_portal_done=save,
                        )  # fmt: skip
                    st.finish_run(run_id, "done", "", tally.as_dict())
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

    def discover_view() -> dict[str, Any]:
        with stores.borrow() as st:
            runs = st.runs()
        # A "running" row counts only while another live process holds the lock;
        # after a restart the row is a leftover and must not block a new run.
        current = runs["current"]
        elsewhere = current is not None and RunLock(settings.lock_file).held_elsewhere()
        running = discover_state.running or elsewhere
        return {
            "running": running,
            "last": runs["last"] or {},
            "current": current if running else None,
        }

    @app.post("/api/discover", dependencies=[Owner])
    def start_discover() -> dict[str, Any]:
        # Same shape as GET /api/discover, which the page reads.
        with discover_state.guard:
            started = not discover_state.running
            discover_state.running = True
        if started:
            threading.Thread(target=_discover, daemon=True).start()
        return {"started": started, **discover_view()}

    def cron(request: Request) -> None:
        """A scheduler holds a token instead of a login."""
        given = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not cfg.cron_token or not hmac.compare_digest(given, cfg.cron_token):
            raise HTTPException(401, "bad cron token")

    @app.post("/api/cron/discover", dependencies=[Depends(cron)])
    def cron_discover() -> dict[str, Any]:
        return start_discover()

    @app.get("/api/cron/discover", dependencies=[Depends(cron)])
    def cron_discover_status() -> dict[str, Any]:
        return discover_view()

    @app.get("/api/discover", dependencies=[Owner])
    def discover_status() -> dict[str, Any]:
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
    def get_profile(st: ST) -> dict[str, Any]:
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
        task_id, task, cancelled = secrets.token_hex(8), _Task(), threading.Event()
        with tasks_lock:
            finished = [k for k, v in tasks.items() if v.status != "running"]
            for key in finished[: max(0, len(tasks) - MAX_TASKS)]:
                del tasks[key]
            tasks[task_id] = task

        def body() -> None:
            try:
                with stores.borrow() as st:
                    finish(task, 0, "", work(st, cancelled))
            except _Abandoned:
                log.warning("background task %s ended after its deadline; nothing saved", task_id)
            except HTTPException as exc:
                finish(task, exc.status_code, str(exc.detail) or "the task failed")
            except Exception as exc:  # report, never crash the worker
                log.exception("background task %s failed", task_id)
                finish(task, 500, f"{type(exc).__name__}: {exc}"[:500])

        def run() -> None:
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

    @app.get("/api/tasks/{task_id}", dependencies=[Owner])
    def task_status(task_id: str) -> dict[str, Any]:
        with tasks_lock:
            task = tasks.get(task_id)
            if task is None:
                raise HTTPException(404, "no such task (the server may have restarted)")
            return asdict(task)

    # --- coach ---

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
        jd_text, title, company = _jd_from(body)

        def work(st: Store, cancelled: threading.Event) -> dict[str, Any]:
            try:
                thinker = cfg.thinker()
                profile = analyze_jd(jd_text, thinker)
                cards = cards_from_summary(summary)
                report = build_report(profile, match_profile(profile, cards), summary, thinker)
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
    def coach_list(st: ST) -> list[dict[str, Any]]:
        return st.sessions()

    @app.get("/api/coach/{session_id}", dependencies=[Owner])
    def coach_get(session_id: int, st: ST) -> dict[str, Any]:
        return _session_view(st, session_id)

    @app.post("/api/coach/{session_id}/chat", dependencies=[Owner])
    def chat(session_id: int, body: ChatIn, st: ST) -> dict[str, str]:
        row = _session(st, session_id)
        history = [Message(m["role"], m["text"]) for m in st.messages(session_id)]
        report, summary = CoachReport.from_json(row["report_json"]), st.profile()["summary"]

        def work(st: Store, cancelled: threading.Event) -> dict[str, Any]:
            try:
                turn = reply(report, summary, history, body.message, cfg.thinker())
            except LLMError as exc:
                raise HTTPException(502, f"AI step failed: {exc}") from exc
            _still_wanted(cancelled)
            st.add_message(session_id, "user", body.message)
            _still_wanted(cancelled)
            st.add_message(session_id, "coach", turn.reply)
            for fact in turn.new_facts[:5]:
                _still_wanted(cancelled)
                st.add_fact(session_id, fact.strip()[:500])
            return {"session_id": session_id}

        return submit(work)

    @app.post("/api/coach/{session_id}/facts/{fact_id}", dependencies=[Owner])
    def confirm_fact(session_id: int, fact_id: int, body: FactIn, st: ST) -> dict[str, bool]:
        if not st.set_fact(session_id, fact_id, body.confirmed):
            raise HTTPException(404, "no such fact")
        return {"ok": True}

    @app.post("/api/coach/{session_id}/resume", dependencies=[Owner])
    def build_resume(session_id: int, st: ST) -> dict[str, str]:
        row = _session(st, session_id)
        prof = st.profile()
        if not prof["base_tex"]:
            raise HTTPException(400, "upload your base resume .tex on the Profile page first")
        confirmed = [f["text"] for f in st.facts(session_id) if f["confirmed"]]
        cards = cards_from_summary("\n".join([prof["summary"], *confirmed]))
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
    def builds(st: ST) -> list[dict[str, Any]]:
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
        try:
            done = subprocess.run(
                [binary, "--untrusted", "--outdir", tmp, str(src)],
                capture_output=True, timeout=timeout, check=False,
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
    from jobagent.config import load_settings
    from jobagent.llm import GeminiLLM
    from jobagent.redact import Redactor, configure_logging

    configure_logging(Redactor.from_environment())  # task tracebacks reach the host's logs
    settings = load_settings()
    model = settings.llm.model
    return create_app(
        WebConfig(
            settings=settings,
            owner_hash=os.environ.get("JOBAGENT_OWNER_HASH", ""),
            session_secret=os.environ.get("JOBAGENT_SESSION_SECRET", ""),
            thinker=lambda: GeminiLLM(model, temperature=0.0),
            writer=lambda: GeminiLLM(model, temperature=0.5),
            cookie_secure=os.environ.get("JOBAGENT_COOKIE_SECURE", "1") != "0",
            cron_token=os.environ.get("JOBAGENT_CRON_TOKEN", ""),
            task_deadline_s=float(os.environ.get("JOBAGENT_TASK_DEADLINE_S", TASK_DEADLINE_S)),
        )
    )
