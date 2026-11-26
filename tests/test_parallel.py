"""Independent AI steps run at the same time, and their usage rows still reach the store
from the calling thread only."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import httpx
import pytest

from jobagent.config import Settings
from jobagent.llm import (
    GeminiLLM,
    LLMError,
    LockedCache,
    StubLLM,
    Usage,
    ask,
    parallel,
    run_scope,
    usage_sink,
)  # fmt: skip
from jobagent.resume.jd import Pass


def test_calls_overlap_and_results_keep_their_order() -> None:
    started = threading.Barrier(3, timeout=2)  # fails unless all three run at once

    def step(n: int) -> int:
        started.wait()
        return n

    assert parallel(*(lambda n=n: step(n) for n in range(3))) == [0, 1, 2]


def test_usage_is_delivered_on_the_calling_thread_with_step_and_run() -> None:
    caller = threading.get_ident()
    seen: list[tuple[str, str, int]] = []

    def record(usage: Usage) -> None:
        seen.append((usage.step, usage.run_id, threading.get_ident()))

    llm = StubLLM(lambda s, u: {"role_title": u})
    with run_scope("run1"), usage_sink(record):
        parallel(
            lambda: ask(llm, Pass, system="s", user="a", step="one"),
            lambda: ask(llm, Pass, system="s", user="b", step="two"),
        )
    assert sorted(seen) == [("one", "run1", caller), ("two", "run1", caller)]


def test_first_error_is_raised_after_every_call_ends() -> None:
    finished: list[str] = []

    def slow() -> str:
        time.sleep(0.05)
        finished.append("slow")
        return "ok"

    def broken() -> str:
        raise LLMError("busy")

    with pytest.raises(LLMError, match="busy"):
        parallel(broken, slow)
    assert finished == ["slow"]


def test_locked_cache_passes_through() -> None:
    class Mem:
        def __init__(self) -> None:
            self.data: dict[str, str] = {}

        def cache_get(self, key: str) -> str | None:
            return self.data.get(key)

        def cache_put(self, key: str, step: str, value: str) -> None:
            self.data[key] = value

    cache = LockedCache(Mem())
    cache.cache_put("k", "step", "v")
    assert cache.cache_get("k") == "v" and cache.cache_get("x") is None


def test_thinking_level_is_sent_and_defaults_to_minimal() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        body = {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}
        return httpx.Response(200, json=body)

    assert Settings().llm.thinking_level == "minimal"
    llm = GeminiLLM.from_settings(Settings().llm, 0.0)
    llm._client = httpx.Client(transport=httpx.MockTransport(handler))
    llm._key = "k"
    llm.complete_json(system="s", user="u")
    assert sent[0]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "minimal"}
