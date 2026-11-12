from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

pytest.importorskip("mcp")

from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from jobagent.config import Settings  # noqa: E402
from jobagent.db import Database  # noqa: E402
from jobagent.llm import StubLLM  # noqa: E402
from jobagent.mcp_server import Jarvis, JarvisError, build_server  # noqa: E402
from jobagent.models import Posting  # noqa: E402
from tests.test_prep import JD, PROFILE, responder  # noqa: E402
from tests.test_web import PASSWORD, client_for  # noqa: E402


def call(server: Any, name: str, **args: Any) -> Any:
    result = asyncio.run(server.call_tool(name, args))
    assert not result.is_error, result.content
    return result.structured_content or json.loads(result.content[0].text)


def owner_client(settings: Settings, **kw: Any) -> Any:
    client = client_for(settings, **kw)
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 200
    return client


@pytest.fixture
def owner(settings: Settings) -> Any:
    db = Database(settings.db_path)
    db.upsert_posting(
        Posting(source="hirist", url="https://www.hirist.tech/j/1", company="Acme",
                title="Backend Engineer", location="Bengaluru", description=JD)
    )  # fmt: skip
    db.close()
    return owner_client(settings, llm=StubLLM(responder))


@pytest.fixture
def jarvis(owner: Any) -> Jarvis:
    key = owner.post("/api/mcp/key").json()["key"]
    owner.cookies.clear()  # from here on only the key speaks
    return Jarvis("http://localhost", key, client=owner, sleep=lambda s: None)


@pytest.fixture
def server(jarvis: Jarvis) -> Any:
    return build_server(jarvis, "https://jobs.example")


def test_tools_work_end_to_end_with_only_the_key(server: Any) -> None:
    found = call(server, "search_jobs", q="backend")
    assert found["total"] == 1
    job_id = found["jobs"][0]["id"]
    assert "description" in call(server, "get_job", job_id=job_id)
    call(server, "mark_job", job_id=job_id, status="interview")
    when = "2026-11-20T15:30:00+05:30"
    call(server, "update_application", job_id=job_id, interview_at=when, note="round 1")
    apps = call(server, "list_applications", stage="interview")
    assert apps["applications"][0]["note"] == "round 1"
    assert call(server, "funded_startups")["startups"] == []


def test_prep_runs_the_background_task_and_links_the_page(server: Any, settings: Settings) -> None:
    job_id = call(server, "search_jobs")["jobs"][0]["id"]
    assert call(server, "interview_prep", job_id=job_id)["prep"] is None
    put = owner_client(settings).put("/api/profile/summary", json={"summary": PROFILE})
    assert put.status_code == 200
    view = call(server, "interview_prep", job_id=job_id, make_new=True)
    assert view["prep"]["pitch"] and view["page"] == f"https://jobs.example/prep/{job_id}"


def test_api_errors_reach_the_client_as_tool_errors(server: Any) -> None:
    with pytest.raises(ToolError, match="no such job"):  # the client sees it as is_error
        asyncio.run(server.call_tool("get_job", {"job_id": 999}))


def test_the_key_opens_only_the_mcp_routes(jarvis: Jarvis) -> None:
    for method, path in (("POST", "/api/mcp/key"), ("PUT", "/api/profile/summary"),
                         ("POST", "/api/kill-switch/on"), ("GET", "/api/profile")):  # fmt: skip
        with pytest.raises(JarvisError, match="refused"):
            jarvis.call(method, path, json={"summary": "x"})


def test_a_new_key_cancels_the_old_one_and_off_cancels_all(settings: Settings) -> None:
    owner = owner_client(settings)
    old = owner.post("/api/mcp/key").json()["key"]
    new = owner.post("/api/mcp/key").json()["key"]
    assert owner.get("/api/mcp/key").json() == {"on": True}
    bare = client_for(settings)
    assert bare.get("/api/tracker", headers={"Authorization": f"Bearer {old}"}).status_code == 401
    assert bare.get("/api/tracker", headers={"Authorization": f"Bearer {new}"}).status_code == 200
    assert owner.delete("/api/mcp/key").status_code == 200
    assert bare.get("/api/tracker", headers={"Authorization": f"Bearer {new}"}).status_code == 401
    assert owner.get("/api/mcp/key").json() == {"on": False}


def test_plain_http_to_a_remote_host_is_refused() -> None:
    with pytest.raises(JarvisError, match="https"):
        Jarvis("http://jobs.example", "x")
