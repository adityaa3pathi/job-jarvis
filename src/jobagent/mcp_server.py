"""MCP server: Claude Code, Antigravity or any MCP client uses job-jarvis through its live
API with the owner's MCP key. It reads, marks and prepares; it never applies or sends."""

from __future__ import annotations

import os
import time
from typing import Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from jobagent.config import load_secrets, load_settings

Stage = Literal["found", "applied", "replied", "interview", "offer", "rejected", "dropped"]
POLL_S = 2.0
TASK_TIMEOUT_S = 240.0
READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
AI = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)


class JarvisError(ToolError):
    """The API refused or failed; the message is safe to show (it never holds the key)."""


class Jarvis:
    """A small client for the job-jarvis API, sending the MCP key on every call."""

    def __init__(
        self, base_url: str, key: str, *, client: httpx.Client | None = None,
        sleep: Any = time.sleep,
    ) -> None:  # fmt: skip
        if not base_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise JarvisError("JOBJARVIS_URL must be an https address")
        self.http = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=60)
        self.headers = {"Authorization": f"Bearer {key}"}
        self.sleep = sleep

    def call(self, method: str, path: str, **kw: Any) -> Any:
        res = self.http.request(method, path, headers=self.headers, **kw)
        if res.status_code == 401:
            raise JarvisError("the MCP key was refused; make a new one on the Profile page")
        if res.status_code >= 400:
            raise JarvisError(f"{method} {path}: {_detail(res)}")
        return res.json()

    def task(self, path: str, body: Any = None) -> Any:
        """Start a slow AI step and wait for its result."""
        task_id = self.call("POST", path, json=body)["task_id"]
        deadline = time.monotonic() + TASK_TIMEOUT_S
        while time.monotonic() < deadline:
            self.sleep(POLL_S)
            state = self.call("GET", f"/api/tasks/{task_id}")
            if state["status"] == "done":
                return state["result"]
            if state["status"] == "failed":
                raise JarvisError(state.get("error") or "the task failed")
        raise JarvisError("still running after 4 minutes; check the website")


def _detail(res: httpx.Response) -> str:
    try:
        detail = res.json().get("detail", "")
    except ValueError:
        detail = ""
    return f"HTTP {res.status_code} {str(detail)[:300]}".strip()


def _brief(job: dict[str, Any]) -> dict[str, Any]:
    keep = ("id", "title", "company", "city", "work_mode", "score", "posted_date", "url", "status")
    out = {k: job.get(k) for k in keep}
    if funded := job.get("funded"):
        out["funded"] = funded["headline"]
    return out


def build_server(jarvis: Jarvis, site_url: str = "") -> MCPServer:
    site = site_url.rstrip("/")
    mcp = MCPServer(
        "job-jarvis",
        instructions=(
            "Tools for the owner's job search in job-jarvis. Jobs, scores and applications "
            "are live data. Never claim a job was applied to: the owner applies themselves, "
            "then marks it here. AI tools (fit_report, interview_prep, build_resume) use the "
            "daily AI budget, so call them only when asked."
        ),
    )

    @mcp.tool(annotations=READ)
    def search_jobs(
        status: Stage = "found", q: str = "", company: str = "", city: str = "",
        role: Literal["", "ai_ml", "full_stack", "backend", "frontend", "mobile", "software",
                      "other"] = "",
        work_mode: Literal["", "remote", "hybrid", "onsite"] = "", max_years: int | None = None,
        posted_within_days: Literal[0, 1, 3, 7, 30] = 0,
        sort: Literal["best", "newest", "fewest_years"] = "best", limit: int = 20,
    ) -> dict[str, Any]:  # fmt: skip
        """Find jobs on the owner's watch list. Each has a 0-100 match score."""
        params: dict[str, Any] = {"status": status, "sort": sort, "limit": max(1, min(limit, 50))}
        given = {
            "q": q, "company": company, "city": city, "role": role, "work_mode": work_mode,
            "max_years": max_years, "posted_within_days": posted_within_days or None,
        }  # fmt: skip
        params |= {k: v for k, v in given.items() if v not in ("", None)}
        page = jarvis.call("GET", "/api/jobs", params=params)
        return {"total": page["total"], "jobs": [_brief(j) for j in page["items"]]}

    @mcp.tool(annotations=READ)
    def get_job(job_id: int) -> dict[str, Any]:
        """One job's full posting text, links, status history and funding news."""
        job: dict[str, Any] = jarvis.call("GET", f"/api/jobs/{job_id}")
        return job

    @mcp.tool(annotations=WRITE)
    def mark_job(job_id: int, status: Stage) -> dict[str, Any]:
        """Move a job: applied, replied, interview, offer, rejected, dropped (skip), or back
        to found. Only after the owner says so; this also trains the ranking."""
        jarvis.call("POST", f"/api/jobs/{job_id}/mark", json={"status": status})
        return {"ok": True, "job_id": job_id, "status": status}

    @mcp.tool(annotations=READ)
    def list_applications(stage: Stage | None = None) -> dict[str, Any]:
        """The Tracker: applications by stage, days with no reply, follow-ups due, interviews."""
        view = jarvis.call("GET", "/api/tracker")
        items = [i for i in view["items"] if stage is None or i["status"] == stage]
        return {"counts": view["counts"], "applications": items}

    @mcp.tool(annotations=WRITE)
    def update_application(
        job_id: int, interview_at: str | None = None, clear_interview: bool = False,
        note: str | None = None, followed_up: bool = False,
    ) -> dict[str, Any]:  # fmt: skip
        """Set an interview time (ISO with offset, e.g. 2026-11-20T15:30:00+05:30), replace
        the note, or record that the owner followed up today."""
        body: dict[str, Any] = {"followed_up": followed_up}
        if interview_at is not None or clear_interview:
            body["interview_at"] = None if clear_interview else interview_at
        if note is not None:
            body["note"] = note
        jarvis.call("PATCH", f"/api/tracker/{job_id}", json=body)
        return {"ok": True, "job_id": job_id}

    @mcp.tool(annotations=READ)
    def funded_startups(only_unwatched: bool = False) -> dict[str, Any]:
        """Startups that raised money in the last 4 months, with open jobs here, if any."""
        rows = jarvis.call("GET", "/api/funding")
        return {"startups": [r for r in rows if not (only_unwatched and r["watched"])]}

    @mcp.tool(annotations=AI)
    def interview_prep(job_id: int, make_new: bool = False) -> dict[str, Any]:
        """Likely interview questions with short answers from the owner's profile. Answers
        with grounded=false quote something the profile lacks: warn the owner."""
        if make_new:
            jarvis.task(f"/api/prep/{job_id}")
        view: dict[str, Any] = jarvis.call("GET", f"/api/prep/{job_id}")
        if view["prep"] is None:
            view["hint"] = "no prep yet: call again with make_new=true"
        view["page"] = f"{site}/prep/{job_id}" if site else ""
        return view

    @mcp.tool(annotations=AI)
    def fit_report(job_id: int) -> dict[str, Any]:
        """Open a Coach session for a job: fit 1-5, what fits, gaps, what to learn."""
        job = jarvis.call("GET", f"/api/jobs/{job_id}")
        if not job["description"]:
            raise JarvisError("this job has no posting text; open it in Coach with its link")
        body = {"jd_text": job["description"][:20_000], "title": job["title"],
                "company": job["company"]}  # fmt: skip
        session_id = jarvis.task("/api/coach", body)["session_id"]
        session: dict[str, Any] = jarvis.call("GET", f"/api/coach/{session_id}")
        report = session.get("report", session)
        return {"session_id": session_id, "report": report,
                "page": f"{site}/coach/{session_id}" if site else ""}  # fmt: skip

    @mcp.tool(annotations=AI)
    def build_resume(session_id: int, instructions: str = "") -> dict[str, Any]:
        """Build a resume tuned to a Coach session's job, from confirmed facts only."""
        done = jarvis.task(f"/api/coach/{session_id}/resume", {"instructions": instructions})
        return {"build_id": done["build_id"], "report": done.get("report", ""),
                "page": f"{site}/resumes" if site else ""}  # fmt: skip

    @mcp.tool(annotations=WRITE)
    def find_new_jobs() -> dict[str, Any]:
        """Start a job scan (takes several minutes), or report the one already running."""
        state: dict[str, Any] = jarvis.call("POST", "/api/discover")
        return state

    return mcp


def main() -> None:
    """Settings come from the env, else the 0600 secrets file the CLI already uses."""
    env = dict(os.environ)
    if not (env.get("JOBJARVIS_URL") and env.get("JOBJARVIS_KEY")):
        env = load_secrets(load_settings(), export=False) | env
    url, key = env.get("JOBJARVIS_URL", ""), env.get("JOBJARVIS_KEY", "")
    if not url or not key:
        raise SystemExit("set JOBJARVIS_URL and JOBJARVIS_KEY in the secrets.env file")
    build_server(Jarvis(url, key), env.get("JOBJARVIS_SITE", url)).run("stdio")


if __name__ == "__main__":
    main()
