from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from jobagent.config import Settings
from jobagent.formfill import FormField, FormPage, suggest
from jobagent.llm import FailingLLM, StubLLM
from tests.test_web import PASSWORD, client_for

SUMMARY = (
    "Built 176 REST APIs with Node.js and TypeScript over PostgreSQL.\n"
    "Built an AI support assistant in Python FastAPI with RAG on pgvector.\n"
    "B.Tech in Electrical Engineering, 2022 to 2026."
)


def page(*fields: FormField, prefs: dict[str, str] | None = None) -> FormPage:
    return FormPage(url="https://jobs.example/apply", fields=list(fields), prefs=prefs or {})


def replying(*items: dict[str, Any]) -> StubLLM:
    return StubLLM(lambda system, user: {"items": list(items)})


def one(f: FormField, item: dict[str, Any], prefs: dict[str, str] | None = None) -> Any:
    return suggest(page(f, prefs=prefs), SUMMARY, [], replying({"id": f.id, **item}))[0]


def test_an_answer_backed_by_a_profile_line_is_a_fact() -> None:
    f = FormField(id="q1", label="Describe your backend work", kind="textarea")
    got = one(f, {"value": "I built 176 REST APIs in Node.js.", "source": "profile", "lines": [1]})
    assert (got.status, got.value) == ("fact", "I built 176 REST APIs in Node.js.")


@pytest.mark.parametrize(
    ("value", "status"),
    [
        ("I built 176 REST APIs with Kafka.", "none"),  # a tool the profile never names
        ("I built 300 REST APIs in Node.js.", "check"),  # a number the profile never states
        ("I led a team building REST APIs.", "none"),  # a bigger role
    ],
)
def test_claims_beyond_the_cited_lines_are_held_back(value: str, status: str) -> None:
    f = FormField(id="q1", label="Tell us about your APIs", kind="textarea")
    got = one(f, {"value": value, "source": "profile", "lines": [1]})
    assert got.status == status
    assert got.value == ("" if status == "none" else value)


def test_a_profile_answer_without_cited_lines_is_dropped() -> None:
    f = FormField(id="q1", label="Backend work", kind="text")
    assert one(f, {"value": "Node.js APIs", "source": "profile", "lines": [99]}).status == "none"


def test_personal_questions_are_left_to_the_owner_and_never_sent() -> None:
    llm = replying({"id": "skills", "value": "Python", "source": "profile", "lines": [2]})
    fields = [
        FormField(id="gender", label="What is your gender?", kind="radio", options=["M", "F"]),
        FormField(id="skills", label="Main language", kind="text"),
    ]
    got = suggest(page(*fields), SUMMARY, [], llm)
    assert [(s.id, s.status) for s in got] == [("gender", "you"), ("skills", "fact")]
    assert "gender" not in llm.calls[0][1]


def test_choices_must_be_one_of_the_options() -> None:
    f = FormField(id="deg", label="Highest degree", kind="select", options=["B.Tech", "M.Tech"])
    assert one(f, {"value": "b.tech", "source": "profile", "lines": [3]}).value == "B.Tech"
    assert one(f, {"value": "BSc", "source": "profile", "lines": [3]}).status == "none"


def test_yes_to_a_tool_needs_a_line_that_names_it() -> None:
    py = FormField(id="py", label="Do you know Python?", kind="radio", options=["Yes", "No"])
    kafka = FormField(id="k", label="Do you know Kafka?", kind="radio", options=["Yes", "No"])
    assert one(py, {"value": "Yes", "source": "profile", "lines": [2]}).status == "fact"
    assert one(kafka, {"value": "Yes", "source": "profile", "lines": [2]}).status == "none"


def test_saved_answers_and_guesses() -> None:
    notice = FormField(id="n", label="Notice period", kind="text")
    pay = FormField(id="p", label="Expected CTC (LPA)", kind="number")
    stack = FormField(id="s", label="Years with Kubernetes", kind="number")
    saved = {"Notice period": "15 days"}
    assert one(notice, {"value": "15 days", "source": "prefs"}, saved).status == "saved"
    assert one(notice, {"value": "30 days", "source": "prefs"}, saved).status == "check"
    assert one(pay, {"value": "12", "source": "guess"}).status == "check"
    assert one(stack, {"value": "3", "source": "guess"}).status == "none"


def test_checkbox_picks_and_long_text_are_shaped() -> None:
    box = FormField(id="c", label="Languages", kind="checkbox", options=["Python", "Go", "Node.js"])
    got = one(box, {"value": "python | node.js", "source": "profile", "lines": [1, 2]})
    assert got.value == "Python | Node.js"
    short = FormField(id="t", label="About you", kind="textarea", max_len=60)
    text = "I built REST APIs with Node.js. I built an AI assistant in Python with RAG."
    assert one(short, {"value": text, "source": "profile", "lines": [1, 2]}).value == (
        "I built REST APIs with Node.js."
    )


def test_form_route_needs_the_extension_token_and_a_summary(settings: Settings) -> None:
    llm = replying(
        {"id": "q1", "value": "I build APIs in Python.", "source": "profile", "lines": [1]}
    )
    c: TestClient = client_for(settings, llm=llm)
    assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200
    body = {"url": "https://x.example", "fields": [{"id": "q1", "label": "Your stack"}]}
    assert c.post("/api/ext/form", json=body).status_code == 401
    h = {"Authorization": f"Bearer {c.post('/api/extension/token').json()['token']}"}
    assert c.post("/api/ext/form", json=body, headers=h).status_code == 400
    c.put("/api/profile/summary", json={"summary": "I build APIs in Python."})
    res = c.post("/api/ext/form", json=body, headers=h)
    assert res.status_code == 200, res.text
    assert res.json()["items"] == [
        {"id": "q1", "value": "I build APIs in Python.", "status": "fact", "note": ""}
    ]


def test_form_route_reports_a_model_failure(settings: Settings) -> None:
    c = client_for(settings, llm=FailingLLM())
    c.post("/api/login", json={"password": PASSWORD})
    c.put("/api/profile/summary", json={"summary": "I build APIs in Python."})
    h = {"Authorization": f"Bearer {c.post('/api/extension/token').json()['token']}"}
    body = {"fields": [{"id": "q1", "label": "Your stack"}]}
    assert c.post("/api/ext/form", json=body, headers=h).status_code == 502


def test_a_vague_label_with_personal_choices_is_still_personal() -> None:
    f = FormField(id="g", label="We value diversity", kind="radio", options=["Female", "Male"])
    vet = FormField(id="v", label="Do any apply?", kind="checkbox", options=["Military veteran"])
    llm = replying()
    assert [s.status for s in suggest(page(f, vet), SUMMARY, [], llm)] == ["you", "you"]
    assert llm.calls == []


def test_tag_boxes_keep_only_tags_the_profile_names() -> None:
    f = FormField(id="sk", label="Enter your skills or area of expertise", kind="chips")
    got = one(f, {"value": "Python, Node.js, Kafka, pgvector", "source": "profile", "lines": []})
    assert (got.status, got.value) == ("fact", "Python, Node.js, pgvector")
    assert "Kafka" in got.note
    lang = FormField(id="l", label="Languages known", kind="chips")
    saved = {"Languages known": "English, Hindi"}
    assert (
        one(lang, {"value": "English, Hindi", "source": "prefs"}, saved).value == "English, Hindi"
    )
    assert one(lang, {"value": "French", "source": "guess"}).status == "none"
