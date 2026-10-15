"""U16: every AI step leaves a trace row, and the steps of one task share a run id."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import BaseModel

from jobagent.config import Settings
from jobagent.llm import (
    MAX_TRACE_OUTPUT,
    LLMError,
    StubLLM,
    Usage,
    ask,
    current_run_id,
    run_scope,
    usage_sink,
)
from jobagent.web.store import Store
from tests.test_ai_cache import WEB_JD, counting, done


class _Echo(BaseModel):
    text: str


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    st = Store(tmp_path / "web.sqlite3")
    yield st
    st.close()


def test_each_call_writes_a_trace_row_with_a_shared_run_id(store: Store) -> None:
    llm = StubLLM(lambda s, u: {"text": u})
    with usage_sink(store.add_usage):
        run = current_run_id()
        ask(llm, _Echo, system="sys A", user="one", step="jd_extract")
        ask(llm, _Echo, system="sys B", user="two", step="coach_report", version="coach-9")
    rows = store.trace_rows(run)
    assert [r["step"] for r in rows] == ["jd_extract", "coach_report"]
    assert run and {r["run_id"] for r in rows} == {run}
    assert rows[0]["verdict"] == "ok" and rows[0]["model"] == "stub"
    assert rows[0]["output"] == '{"text": "one"}'
    assert rows[1]["prompt_version"] == "coach-9"
    assert rows[0]["prompt_version"].startswith("sys-")
    assert rows[0]["input_hash"] != rows[1]["input_hash"]
    assert rows[0]["tokens_in"] > 0 and rows[0]["at"].endswith("+05:30")
    assert len(store.usage_rows()) == 2  # U5 usage rows still written


def test_same_prompt_and_input_give_the_same_hash_and_version(store: Store) -> None:
    llm = StubLLM(lambda s, u: {"text": "x"})
    with usage_sink(store.add_usage):
        ask(llm, _Echo, system="sys", user="same", step="a")
        ask(llm, _Echo, system="sys", user="same", step="a")
    one, two = store.trace_rows()
    assert one["input_hash"] == two["input_hash"]
    assert one["prompt_version"] == two["prompt_version"]


def test_separate_sinks_get_separate_runs(store: Store) -> None:
    llm = StubLLM(lambda s, u: {"text": "x"})
    runs = []
    for _ in range(2):
        with usage_sink(store.add_usage):
            runs.append(current_run_id())
            ask(llm, _Echo, system="s", user="u", step="a")
    assert runs[0] != runs[1]
    assert [len(store.trace_rows(r)) for r in runs] == [1, 1]
    assert current_run_id() == ""


def test_an_outer_run_scope_is_kept_by_the_sink(store: Store) -> None:
    llm = StubLLM(lambda s, u: {"text": "x"})
    with run_scope("cli-run"), usage_sink(store.add_usage):
        ask(llm, _Echo, system="s", user="u", step="a")
    assert [r["run_id"] for r in store.trace_rows("cli-run")] == ["cli-run"]


def test_malformed_output_is_traced_as_malformed(store: Store) -> None:
    llm = StubLLM(lambda s, u: "not json")
    with usage_sink(store.add_usage), pytest.raises(LLMError):
        ask(llm, _Echo, system="s", user="u", step="a")
    (row,) = store.trace_rows()
    assert row["verdict"] == "malformed" and row["output"] == "not json"


def test_long_output_is_cut_to_4k(store: Store) -> None:
    llm = StubLLM(lambda s, u: {"text": "y" * 10_000})
    with usage_sink(store.add_usage):
        ask(llm, _Echo, system="s", user="u", step="a")
    (row,) = store.trace_rows()
    assert len(row["output"]) == MAX_TRACE_OUTPUT == 4000


def test_usage_outside_ask_still_reaches_the_sink() -> None:
    seen: list[Usage] = []
    with usage_sink(seen.append):
        StubLLM(lambda s, u: {"text": "x"}).complete_json(system="s", user="u")
    assert len(seen) == 1 and seen[0].run_id == ""


def test_one_web_task_shares_one_run_id(settings: Settings) -> None:
    c, _ = counting(settings)
    done(c, c.post("/api/coach", json={"jd_text": WEB_JD}))
    done(c, c.post("/api/coach", json={"jd_text": WEB_JD + " Go."}))
    st = Store(settings.db_path)
    try:
        rows = st.trace_rows()
    finally:
        st.close()
    # The second report is a cache hit, so its task makes one call.
    assert [r["step"] for r in rows] == ["jd_extract", "coach_report", "jd_extract"]
    assert rows[0]["run_id"] == rows[1]["run_id"] != rows[2]["run_id"]
