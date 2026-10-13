"""FastAPI routes. Every route except login and health needs the owner's session cookie."""

from __future__ import annotations

import hmac
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from jobagent.config import REPO_ROOT, Settings, ensure_data_dir
from jobagent.db import Database
from jobagent.discovery import discover_all
from jobagent.discovery.base import Fetcher, load_companies, load_searches
from jobagent.discovery.fetch import HttpFetcher, Resolver, system_resolver
from jobagent.discovery.manual import fetch_posting
from jobagent.llm import LLM, LLMError
from jobagent.lock import LockHeldError, RunLock
from jobagent.policy import Policy, clear_kill_switch, engage_kill_switch
from jobagent.resume.coach import MAX_JD_CHARS, CoachReport, Message, build_report, reply
from jobagent.resume.evidence import cards_from_summary
from jobagent.resume.jd_agent import analyze_jd
from jobagent.resume.latex import LatexError
from jobagent.resume.match import match_profile
from jobagent.resume.pipeline import run_tailor
from jobagent.web.auth import COOKIE, SESSION_TTL_S, make_token, token_valid, verify_password
from jobagent.web.store import Store, profile_from_json, profile_to_json
from jobagent.web.uploads import UploadError, doc_to_text

MAX_LOGIN_FAILURES = 5
LOGIN_WINDOW_S = 900
PDF_TIMEOUT_S = 120
MAX_TASKS = 50
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


@dataclass
class _Task:
    status: str = "running"  # running | done | failed
    result: dict[str, Any] | None = None
    error: str = ""
    code: int = 0


@dataclass
class _DiscoverState:
    running: bool = False
    last: dict[str, Any] = field(default_factory=dict)
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


def _jobs_db(request: Request) -> Iterator[Database]:
    settings: Settings = request.app.state.settings
    ensure_data_dir(settings)
    db = Database(settings.db_path, settings.max_years_asked)
    try:
        yield db
    finally:
        db.close()


def _store(request: Request) -> Iterator[Store]:
    settings: Settings = request.app.state.settings
    ensure_data_dir(settings)
    st = Store(settings.db_path)
    try:
        yield st
    finally:
        st.close()


# Module level: with postponed annotations FastAPI resolves these names from module globals.
DB = Annotated[Database, Depends(_jobs_db)]
ST = Annotated[Store, Depends(_store)]


def create_app(cfg: WebConfig) -> FastAPI:
    if not cfg.owner_hash or not cfg.session_secret:
        raise RuntimeError("JOBAGENT_OWNER_HASH and JOBAGENT_SESSION_SECRET must be set")
    app = FastAPI(title="job-agent", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = cfg.settings
    settings = cfg.settings
    failures: list[float] = []
    discover_state = _DiscoverState()

    def owner(request: Request) -> None:
        token = request.cookies.get(COOKIE, "")
        if not token or not token_valid(cfg.session_secret, token):
            raise HTTPException(401, "login needed")

    Owner = Depends(owner)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"ok": "yes"}

    @app.post("/api/login")
    def login(body: Login, response: Response) -> dict[str, bool]:
        now = time.time()
        failures[:] = [t for t in failures if now - t < LOGIN_WINDOW_S]
        if len(failures) >= MAX_LOGIN_FAILURES:
            raise HTTPException(429, "too many attempts, wait 15 minutes")
        if not verify_password(body.password, cfg.owner_hash):
            failures.append(now)
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
    def me() -> dict[str, Any]:
        return {"ok": True, "kill_switch": settings.stop_file.exists()}

    # --- jobs and discovery ---

    @app.get("/api/jobs", dependencies=[Owner])
    def jobs(db: DB, status: str | None = None, source: str | None = None) -> list[dict[str, Any]]:
        return [dict(r) for r in db.list_jobs(status=status, source=source)]

    @app.post("/api/jobs/{job_id}/mark", dependencies=[Owner])
    def mark(job_id: int, body: Mark, db: DB) -> dict[str, bool]:
        if db.get_job(job_id) is None:
            raise HTTPException(404, "no such job")
        db.set_status(job_id, body.status, "owner")
        return {"ok": True}

    def _discover() -> None:
        lock = RunLock(settings.lock_file)
        try:
            lock.acquire()
        except LockHeldError as exc:
            discover_state.last = {"error": str(exc)}
            discover_state.running = False
            return
        try:
            policy = Policy.load(REPO_ROOT / "policy.yaml", settings.stop_file)
            result = discover_all(
                load_companies(REPO_ROOT / "companies.yaml"), cfg.fetcher, policy,
                load_searches(REPO_ROOT / "searches.yaml"),
            )  # fmt: skip
            ensure_data_dir(settings)
            db = Database(settings.db_path, settings.max_years_asked)
            try:
                new = sum(
                    1 for p in result.postings
                    if (r := db.upsert_posting(p)).outcome == "new" and r.status == "found"
                )  # fmt: skip
            finally:
                db.close()
            discover_state.last = {
                "read": len(result.postings), "new": new,
                "unreachable": [f"{u.company}: {u.reason}" for u in result.unreachable],
                "skipped": result.skipped_by_policy, "finished_at": time.time(),
            }  # fmt: skip
        except Exception as exc:  # report, never crash the server thread
            discover_state.last = {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            lock.release()
            discover_state.running = False

    @app.post("/api/discover", dependencies=[Owner])
    def start_discover() -> dict[str, Any]:
        with discover_state.guard:
            if discover_state.running:
                return {"started": False, "running": True}
            discover_state.running = True
        threading.Thread(target=_discover, daemon=True).start()
        return {"started": True, "running": True}

    @app.post("/api/cron/discover")
    def cron_discover(request: Request) -> dict[str, Any]:
        """Daily trigger from a scheduler that holds a token instead of a login."""
        given = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not cfg.cron_token or not hmac.compare_digest(given, cfg.cron_token):
            raise HTTPException(401, "bad cron token")
        return start_discover()

    @app.get("/api/discover", dependencies=[Owner])
    def discover_status() -> dict[str, Any]:
        return {"running": discover_state.running, "last": discover_state.last}

    @app.get("/api/searches", dependencies=[Owner])
    def searches() -> list[dict[str, str]]:
        return [s.model_dump() for s in load_searches(REPO_ROOT / "searches.yaml")]

    @app.post("/api/kill-switch/{state}", dependencies=[Owner])
    def kill_switch(state: Literal["on", "off"]) -> dict[str, bool]:
        (engage_kill_switch if state == "on" else clear_kill_switch)(settings.stop_file)
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

    @app.post("/api/profile/summary-file", dependencies=[Owner])
    async def upload_summary(st: ST, file: Annotated[UploadFile, File()]) -> dict[str, Any]:
        try:
            text = doc_to_text(file.filename or "", await file.read())
        except UploadError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not text:
            raise HTTPException(400, "no text found in the file")
        st.save_profile(summary=text)
        return {"chars": len(text), "summary": text}

    @app.post("/api/profile/tex", dependencies=[Owner])
    async def upload_tex(st: ST, file: Annotated[UploadFile, File()]) -> dict[str, int]:
        if not (file.filename or "").lower().endswith(".tex"):
            raise HTTPException(400, "upload a .tex file")
        try:
            text = doc_to_text(file.filename or "", await file.read())
        except UploadError as exc:
            raise HTTPException(400, str(exc)) from exc
        st.save_profile(base_tex=text)
        return {"chars": len(text)}

    # --- background tasks: AI steps outlast proxy timeouts, so the page polls instead ---

    tasks: dict[str, _Task] = {}
    pool = ThreadPoolExecutor(max_workers=2)

    def submit(work: Callable[[Store], dict[str, Any]]) -> dict[str, str]:
        finished = [k for k, v in tasks.items() if v.status != "running"]
        for key in finished[: max(0, len(tasks) - MAX_TASKS)]:
            del tasks[key]
        task_id, task = secrets.token_hex(8), _Task()
        tasks[task_id] = task

        def run() -> None:
            st = Store(settings.db_path)
            try:
                task.result, task.status = work(st), "done"
            except HTTPException as exc:
                task.status, task.code, task.error = "failed", exc.status_code, str(exc.detail)
            except Exception as exc:  # report, never crash the worker
                task.status, task.code, task.error = "failed", 500, type(exc).__name__
            finally:
                st.close()

        pool.submit(run)
        return {"task_id": task_id}

    @app.get("/api/tasks/{task_id}", dependencies=[Owner])
    def task_status(task_id: str) -> dict[str, Any]:
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
        policy = Policy.load(REPO_ROOT / "policy.yaml", settings.stop_file)
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

        def work(st: Store) -> dict[str, Any]:
            try:
                thinker = cfg.thinker()
                profile = analyze_jd(jd_text, thinker)
                cards = cards_from_summary(summary)
                report = build_report(profile, match_profile(profile, cards), summary, thinker)
            except LLMError as exc:
                raise HTTPException(502, f"AI step failed: {exc}") from exc
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

        def work(st: Store) -> dict[str, Any]:
            try:
                turn = reply(report, summary, history, body.message, cfg.thinker())
            except LLMError as exc:
                raise HTTPException(502, f"AI step failed: {exc}") from exc
            st.add_message(session_id, "user", body.message)
            st.add_message(session_id, "coach", turn.reply)
            for fact in turn.new_facts[:5]:
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

        def work(st: Store) -> dict[str, Any]:
            try:
                outcome = run_tailor(
                    jd_text, prof["base_tex"], cards, cfg.thinker(),
                    writer_llm=cfg.writer(), profile=profile,
                )  # fmt: skip
            except (LatexError, LLMError) as exc:
                raise HTTPException(502, f"resume build failed: {exc}") from exc
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

    @app.get("/api/builds/{build_id}/pdf", dependencies=[Owner])
    def build_pdf(build_id: int, st: ST) -> Response:
        tex = _build(st, build_id)["tex"]
        return Response(
            compile_pdf(tex), media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="resume-{build_id}.pdf"'},
        )  # fmt: skip

    return app


# Tectonic runs XeTeX; common resume templates call these pdfTeX-only commands.
XETEX_SHIM = (
    "\\providecommand{\\pdfglyphtounicode}[2]{}\n"
    "\\ifdefined\\pdfgentounicode\\else\\newcount\\pdfgentounicode\\fi\n"
)


def compile_pdf(tex: str) -> bytes:
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
                capture_output=True, timeout=PDF_TIMEOUT_S, check=False,
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
        )
    )
