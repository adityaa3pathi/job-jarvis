from __future__ import annotations

import httpx
import pytest

from jobagent.llm import MAX_RATE_RETRIES, GeminiLLM, LLMError


def ok() -> httpx.Response:
    return httpx.Response(
        200, json={"candidates": [{"content": {"parts": [{"text": '{"ok": 1}'}]}}]}
    )


def gemini(replies: list[httpx.Response | Exception]) -> tuple[GeminiLLM, list[float], list[int]]:
    waits: list[float] = []
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    llm = GeminiLLM("m", api_key="k", transport=httpx.MockTransport(handler), sleep=waits.append)
    return llm, waits, calls


def test_503_then_success_is_retried_with_a_wait() -> None:
    llm, waits, _ = gemini([httpx.Response(503, text="overloaded"), ok()])
    assert llm.complete_json(system="s", user="u") == '{"ok": 1}'
    assert len(waits) == 1 and waits[0] > 0


def test_timeout_then_success_is_retried() -> None:
    llm, waits, _ = gemini([httpx.ReadTimeout("slow"), ok()])
    assert llm.complete_json(system="s", user="u") == '{"ok": 1}'
    assert len(waits) == 1


def test_waits_grow_between_5xx_retries() -> None:
    llm, waits, _ = gemini([httpx.Response(500), httpx.Response(502), httpx.Response(504), ok()])
    llm.complete_json(system="s", user="u")
    assert len(waits) == 3 and waits[0] < waits[1] < waits[2]


def test_lasting_503_gives_up_and_names_the_status_and_cause() -> None:
    body = {"error": {"code": 503, "message": "The model is overloaded."}}
    replies: list[httpx.Response | Exception] = [
        httpx.Response(503, json=body) for _ in range(MAX_RATE_RETRIES + 1)
    ]
    llm, _, calls = gemini(replies)
    with pytest.raises(LLMError) as err:
        llm.complete_json(system="s", user="u")
    assert "503" in str(err.value) and "overloaded" in str(err.value)
    assert len(calls) == MAX_RATE_RETRIES + 1


def test_lasting_timeout_gives_up_with_a_timeout_error() -> None:
    replies: list[httpx.Response | Exception] = [
        httpx.ReadTimeout("slow") for _ in range(MAX_RATE_RETRIES + 1)
    ]
    llm, _, calls = gemini(replies)
    with pytest.raises(LLMError, match="timed out"):
        llm.complete_json(system="s", user="u")
    assert len(calls) == MAX_RATE_RETRIES + 1


def test_bad_request_is_not_retried_and_keeps_its_status() -> None:
    llm, waits, _ = gemini([httpx.Response(404, text="model not found")])
    with pytest.raises(LLMError, match="404.*model not found"):
        llm.complete_json(system="s", user="u")
    assert waits == []


def test_long_rate_limit_says_quota() -> None:
    llm, waits, _ = gemini([httpx.Response(429, text="Please retry in 3600s.")])
    with pytest.raises(LLMError, match="429.*quota"):
        llm.complete_json(system="s", user="u")
    assert waits == []


def test_blocked_reply_names_the_reason() -> None:
    llm, _, _ = gemini([httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})])
    with pytest.raises(LLMError, match="SAFETY"):
        llm.complete_json(system="s", user="u")


def test_empty_candidate_names_the_finish_reason() -> None:
    llm, _, _ = gemini([httpx.Response(200, json={"candidates": [{"finishReason": "MAX_TOKENS"}]})])
    with pytest.raises(LLMError, match="MAX_TOKENS"):
        llm.complete_json(system="s", user="u")
