from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jobagent import cli
from jobagent.llm import StubLLM
from jobagent.resume import cli_tailor
from jobagent.resume.evidence import cards_from_summary
from jobagent.resume.pipeline import run_tailor

SAMPLE = (Path(__file__).parent / "fixtures" / "resume" / "sample.tex").read_text()
JD = "Senior AI Engineer\n5+ years. Python and Terraform are required. Langfuse is nice to have.\n"
EXTRACT: dict[str, Any] = {
    "role_title": "Senior AI Engineer",
    "level": "senior",
    "years_min": 5,
    "skills": [
        {"name": "Python", "importance": "must"},
        {"name": "Terraform", "importance": "must"},
        {"name": "Langfuse", "importance": "nice"},
    ],
    "implicit_skills": [],
}


def fake(system: str, user: str) -> Any:
    if "Rewrite each resume bullet" in system:
        return {"items": []}
    return EXTRACT


CARDS = cards_from_summary("I build APIs in Python.\nI trace LLM apps with Langfuse.")


def test_pipeline_report_lists_gap_skills_and_prep() -> None:
    out = run_tailor(JD, SAMPLE, CARDS, StubLLM(fake), years_have=2)
    assert out.tailored.added_gap_skills == ["Terraform"]
    assert "NOT backed by your work" in out.report
    assert "- Terraform" in out.report
    assert "Fit: Posting asks for 5+ years" in out.report
    assert out.keywords_after > out.keywords_before


def test_no_gap_skills_switch_keeps_the_resume_honest() -> None:
    out = run_tailor(JD, SAMPLE, CARDS, StubLLM(fake), add_gaps=False)
    assert "Terraform" not in out.tailored.latex
    assert out.tailored.added_gap_skills == []


def test_cli_writes_resume_and_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "jd.txt").write_text(JD)
    (tmp_path / "r.tex").write_text(SAMPLE)
    (tmp_path / "me.txt").write_text("I build APIs in Python.\n")
    monkeypatch.setattr(cli_tailor, "build_llms", lambda: (StubLLM(fake), StubLLM(fake)))
    code = cli.main(
        ["tailor", "--jd", str(tmp_path / "jd.txt"), "--latex", str(tmp_path / "r.tex"),
         "--summary", str(tmp_path / "me.txt"), "--out", str(tmp_path / "out")]
    )  # fmt: skip
    assert code == 0
    assert (tmp_path / "out" / "senior-ai-engineer.tex").read_text().startswith("\\documentclass")
    assert "Tailoring report" in (tmp_path / "out" / "senior-ai-engineer-report.md").read_text()


def test_cli_without_evidence_stops(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "jd.txt").write_text(JD)
    (tmp_path / "r.tex").write_text(SAMPLE)
    code = cli.main(
        ["tailor", "--jd", str(tmp_path / "jd.txt"), "--latex", str(tmp_path / "r.tex")]
    )
    assert code == 2
    assert "No evidence" in capsys.readouterr().err


def test_cli_reports_a_broken_latex_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "jd.txt").write_text(JD)
    (tmp_path / "r.tex").write_text("not latex")
    (tmp_path / "me.txt").write_text("I use Python.\n")
    monkeypatch.setattr(cli_tailor, "build_llms", lambda: (StubLLM(fake), StubLLM(fake)))
    code = cli.main(
        [
            "tailor",
            "--jd",
            str(tmp_path / "jd.txt"),
            "--latex",
            str(tmp_path / "r.tex"),
            "--summary",
            str(tmp_path / "me.txt"),
        ]
    )
    assert code == 1
    assert "LatexError" in capsys.readouterr().err


def test_gemini_waits_and_retries_after_rate_limit() -> None:
    import httpx

    from jobagent.llm import GeminiLLM

    replies = [
        httpx.Response(429, text="quota. Please retry in 2.5s."),
        httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]}
        ),
    ]
    waits: list[float] = []
    transport = httpx.MockTransport(lambda request: replies.pop(0))
    llm = GeminiLLM("m", api_key="k", transport=transport, sleep=waits.append)
    assert llm.complete_json(system="s", user="u") == '{"ok": true}'
    assert waits == [3.5]


def test_report_lists_skills_removed_from_the_resume() -> None:
    out = run_tailor(JD, SAMPLE, CARDS, StubLLM(fake))
    assert "## Removed from your Skills" in out.report
    assert "Kubernetes" in out.tailored.dropped_skills
    for name in out.tailored.dropped_skills:
        assert f"- {name}" in out.report


def test_report_says_when_bullet_rewrites_failed() -> None:
    def broken_writer(system: str, user: str) -> Any:
        if "Rewrite each resume bullet" in system:
            raise TimeoutError("stubbed timeout")
        return fake(system, user)

    out = run_tailor(JD, SAMPLE, CARDS, StubLLM(fake), writer_llm=StubLLM(broken_writer))
    assert "bullets kept unchanged: model error" in out.report
    assert "TimeoutError" in out.report


def test_report_says_when_job_analysis_calls_failed() -> None:
    seen: list[int] = []

    def flaky(system: str, user: str) -> Any:
        if "list what it asks for" in system:
            seen.append(1)
            if len(seen) == 1:
                raise TimeoutError("stubbed timeout")
        return fake(system, user)

    out = run_tailor(JD, SAMPLE, CARDS, StubLLM(flaky))
    assert "1 of" in out.report and "job-analysis model calls failed" in out.report
    assert "TimeoutError" in out.report
