"""Daily Telegram digest (U15). Telegram is never called for real: a fake transport answers."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from jobagent import cli
from jobagent.config import Settings
from jobagent.db import Database
from jobagent.digest import (
    MAX_MESSAGE,
    TelegramSender,
    format_digest,
    send_digest,
    sender_from_env,
)
from jobagent.models import Posting
from jobagent.web.store import Store

TOKEN = "123456:ABCDEFGHIJKLMNOPQRSTUV"


class FakeTelegram:
    def __init__(self, status: int = 200) -> None:
        self.sent: list[dict[str, Any]] = []
        self.urls: list[str] = []
        self.status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        self.sent.append(json.loads(request.content))
        if self.status != 200:
            return httpx.Response(self.status, json={"ok": False, "description": "chat not found"})
        return httpx.Response(200, json={"ok": True, "result": {}})

    def sender(self) -> TelegramSender:
        return TelegramSender(TOKEN, "42", transport=httpx.MockTransport(self.handler))


def ranked(settings: Settings, n: int, **kw: Any) -> Database:
    db = Database(settings.db_path)
    db.upsert_many([
        Posting(source="greenhouse", url=f"https://x.example/{i}", company=f"Co{i}",
                title=f"AI Engineer {i}", location="Bengaluru", **kw)
        for i in range(n)
    ])  # fmt: skip
    db.conn.executemany(
        "UPDATE jobs SET score=?, rank_reason=? WHERE id=?",
        [(50 + i, f"stack {i} of 9 tools", i + 1) for i in range(n)],
    )
    db.conn.commit()
    return db


def item(**kw: Any) -> dict[str, Any]:
    base = {
        "id": 1, "title": "AI Engineer", "company": "Acme", "location": "Pune", "score": 80,
        "rank_reason": "stack 4 of 5 tools", "url": "https://x.example/1",
    }  # fmt: skip
    return base | kw


def test_format_escapes_html_and_shows_every_field() -> None:
    [text] = format_digest(
        [item(title="<b>Dev</b> & AI", company="R&D <Co>", url='https://x.example/?a=1&b="2"')],
        "15-10-2026",
    )
    assert "&lt;b&gt;Dev&lt;/b&gt; &amp; AI" in text and "R&amp;D &lt;Co&gt;" in text
    assert 'href="https://x.example/?a=1&amp;b=&quot;2&quot;"' in text
    assert "Pune" in text and "80/100" in text and "stack 4 of 5 tools" in text
    assert "15-10-2026" in text and "<b>Dev</b>" not in text


def test_format_refuses_non_http_links() -> None:
    [text] = format_digest([item(url="javascript:alert(1)")], "15-10-2026")
    assert "javascript:" not in text and "href" not in text


def test_long_digests_are_chunked_under_the_limit_on_job_boundaries() -> None:
    jobs = [item(id=i, title="T" * 150, rank_reason="r" * 200, url=f"https://x.example/{i}")
            for i in range(60)]  # fmt: skip
    chunks = format_digest(jobs, "15-10-2026")
    assert len(chunks) > 1 and all(len(c) <= MAX_MESSAGE for c in chunks)
    joined = "".join(chunks)
    assert all(f'https://x.example/{i}"' in joined for i in range(60))
    assert all(c.count("<b>") == c.count("</b>") for c in chunks)


def test_sender_needs_both_env_values() -> None:
    assert sender_from_env({}) is None
    assert sender_from_env({"TELEGRAM_BOT_TOKEN": TOKEN}) is None
    assert sender_from_env({"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"}) is not None


def test_digest_sends_top_jobs_once_per_run(settings: Settings) -> None:
    db, tg = ranked(settings, 5), FakeTelegram()
    out = send_digest(db, 7, tg.sender(), limit=3)
    assert out["sent"] == 3 and len(tg.sent) == 1
    msg = tg.sent[0]
    assert msg["chat_id"] == "42" and msg["parse_mode"] == "HTML"
    assert tg.urls[0].endswith(f"/bot{TOKEN}/sendMessage")
    assert msg["text"].index("AI Engineer 4") < msg["text"].index("AI Engineer 3")
    assert "AI Engineer 1" not in msg["text"]
    again = send_digest(db, 7, tg.sender(), limit=3)
    assert again["sent"] == 0 and "already sent" in again["skipped"] and len(tg.sent) == 1
    # The next run sends only jobs no digest has shown yet.
    send_digest(db, 8, tg.sender(), limit=3)
    assert "AI Engineer 4" not in tg.sent[1]["text"] and "AI Engineer 1" in tg.sent[1]["text"]


def test_failed_send_can_be_retried(settings: Settings) -> None:
    db, tg = ranked(settings, 2), FakeTelegram(status=400)
    out = send_digest(db, 1, tg.sender(), limit=5)
    assert out["sent"] == 0 and "chat not found" in out["error"]
    assert TOKEN not in out["error"]
    tg.status = 200
    assert send_digest(db, 1, tg.sender(), limit=5)["sent"] == 2


def test_digest_skipped_quietly_when_not_configured(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    db = ranked(settings, 2)
    with caplog.at_level("INFO"):
        out = send_digest(db, 1, None, limit=5)
    assert out == {"sent": 0, "skipped": "Telegram not configured"}
    assert "TELEGRAM_BOT_TOKEN" in caplog.text
    tg = FakeTelegram()
    assert send_digest(db, 1, tg.sender(), limit=5)["sent"] == 2  # nothing was recorded


def test_cli_digest_uses_the_last_finished_run(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    ranked(settings, 2).close()
    st = Store(settings.db_path)
    run = st.start_run()
    st.finish_run(run, "done", "", {})
    st.close()
    tg = FakeTelegram()
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "sender_from_env", lambda env: tg.sender())
    assert cli.main(["digest"]) == 0
    assert cli.main(["digest"]) == 0
    assert len(tg.sent) == 1
    assert "already sent" in capsys.readouterr().out
