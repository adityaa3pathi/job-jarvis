from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from jobagent.config import LLMSettings
from jobagent.llm import (
    BedrockLLM,
    FailingLLM,
    FallbackLLM,
    GeminiLLM,
    LLMError,
    StubLLM,
    Usage,
    ask,
    make_llm,
    model_id,
    usage_sink,
)


class _Reply(BaseModel):
    ok: bool


class AwsError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(f"An error occurred ({code})")
        self.response = {"Error": {"Code": code}}


class FakeBedrock:
    def __init__(self, *replies: dict[str, Any] | Exception) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    def converse(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def said(text: str) -> dict[str, Any]:
    return {
        "output": {"message": {"content": [{"text": text}]}},
        "usage": {"inputTokens": 12, "outputTokens": 3},
        "stopReason": "end_turn",
    }


def bedrock(fake: FakeBedrock) -> BedrockLLM:
    return BedrockLLM("amazon.nova-2-lite-v1:0", client=fake, sleep=lambda s: None)


def test_bedrock_answers_and_reports_usage() -> None:
    fake = FakeBedrock(said('```json\n{"ok": true}\n```'))
    seen: list[Usage] = []
    with usage_sink(seen.append):
        got = ask(bedrock(fake), _Reply, system="S", user="U", step="jd_extract")
    assert got.ok
    request = fake.requests[0]
    assert request["system"][0]["text"].endswith("S")
    assert request["messages"][0]["content"][0]["text"] == "U"
    assert (seen[0].model, seen[0].tokens_in, seen[0].tokens_out) == (
        "amazon.nova-2-lite-v1:0", 12, 3,
    )  # fmt: skip


def test_bedrock_waits_out_throttling_but_not_other_errors() -> None:
    fake = FakeBedrock(AwsError("ThrottlingException"), said('{"ok": true}'))
    assert bedrock(fake).complete_json(system="S", user="U") == '{"ok": true}'
    with pytest.raises(LLMError, match="AccessDeniedException"):
        bedrock(FakeBedrock(AwsError("AccessDeniedException"))).complete_json(system="S", user="U")


def test_a_used_up_daily_quota_fails_at_once() -> None:
    daily = AwsError("ThrottlingException")
    daily.args = ("Too many tokens per day, please wait before trying again.",)
    fake = FakeBedrock(daily, said('{"ok": true}'))
    with pytest.raises(LLMError, match="per day"):
        bedrock(fake).complete_json(system="S", user="U")
    assert len(fake.requests) == 1


def test_empty_bedrock_reply_is_an_error() -> None:
    with pytest.raises(LLMError, match="no text"):
        bedrock(FakeBedrock(said("  "))).complete_json(system="S", user="U")


def test_fallback_uses_the_next_model_only_when_the_first_fails() -> None:
    first = StubLLM(lambda s, u: {"ok": True})
    chain = FallbackLLM(first, FailingLLM())
    assert ask(chain, _Reply, system="S", user="U").ok
    rescued = FallbackLLM(
        FailingLLM(LLMError("Gemini HTTP 429")), StubLLM(lambda s, u: {"ok": True})
    )
    assert ask(rescued, _Reply, system="S", user="U").ok
    with pytest.raises(LLMError, match="429"):
        FallbackLLM(FailingLLM(LLMError("Gemini HTTP 429")), FailingLLM()).complete_json(
            system="S", user="U"
        )


def test_cache_keys_keep_the_first_model_id() -> None:
    chain = FallbackLLM(GeminiLLM("gemini-x", api_key="k"), bedrock(FakeBedrock()))
    assert model_id(chain) == "gemini-x"


def test_make_llm_follows_settings() -> None:
    assert isinstance(make_llm(LLMSettings(), 0.0), GeminiLLM)
    both = make_llm(LLMSettings(bedrock_model="amazon.nova-2-lite-v1:0"), 0.0)
    assert isinstance(both, FallbackLLM) and [type(m) for m in both.chain] == [
        GeminiLLM, BedrockLLM,
    ]  # fmt: skip
    only = make_llm(LLMSettings(provider="bedrock", bedrock_model="amazon.nova-2-lite-v1:0"), 0.0)
    assert isinstance(only, BedrockLLM)
