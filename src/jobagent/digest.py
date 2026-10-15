"""Daily digest (U15, R21): the best new ranked jobs, sent to the owner's Telegram chat.
Sent once per discovery run; quietly skipped when the bot settings are missing."""

from __future__ import annotations

import html
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

import httpx

from jobagent.db import IST, Database

log = logging.getLogger(__name__)
MAX_MESSAGE = 4096  # Telegram's limit for one message
API = "https://api.telegram.org"
MAX_URL = 1000


class DigestError(Exception):
    """Telegram refused or could not be reached. The message never holds the bot token."""


class Sender(Protocol):
    def send(self, text: str) -> None: ...


class TelegramSender:
    def __init__(
        self, token: str, chat_id: str, *, transport: httpx.BaseTransport | None = None,
        timeout_s: float = 15.0,
    ) -> None:  # fmt: skip
        self._token, self.chat_id = token, chat_id
        self._client = httpx.Client(timeout=timeout_s, transport=transport)

    def send(self, text: str) -> None:
        body = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                "disable_web_page_preview": True}  # fmt: skip
        try:
            res = self._client.post(f"{API}/bot{self._token}/sendMessage", json=body)
        except httpx.HTTPError as exc:  # its text holds the URL, and so the token
            raise DigestError(f"Telegram unreachable: {type(exc).__name__}") from None
        if res.status_code != 200:
            try:
                why = str(res.json().get("description", ""))
            except ValueError:
                why = ""
            raise DigestError(f"Telegram HTTP {res.status_code}: {why[:200]}")


def sender_from_env(env: Mapping[str, str]) -> TelegramSender | None:
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = env.get("TELEGRAM_CHAT_ID", "").strip()
    return TelegramSender(token, chat) if token and chat else None


def _block(n: int, job: Mapping[str, Any]) -> str:
    title = html.escape(str(job["title"])[:120], quote=False)
    url = str(job.get("url") or "")
    if url.startswith(("https://", "http://")) and len(url) <= MAX_URL:
        title = f'<a href="{html.escape(url, quote=True)}">{title}</a>'
    company = html.escape(str(job["company"])[:80], quote=False)
    where = html.escape(str(job.get("location") or "location not stated")[:80], quote=False)
    reason = html.escape(str(job.get("rank_reason") or "")[:200], quote=False)
    lines = [f"{n}. <b>{title}</b> · {company}", f"{where} · {job['score']}/100"]
    return "\n".join([*lines, *([f"<i>{reason}</i>"] if reason else [])])


def _messages(
    jobs: Sequence[Mapping[str, Any]], head: str | None, first: int = 1
) -> list[tuple[str, list[Any]]]:
    """(HTML, job ids) per message, each under Telegram's limit, split only between jobs."""
    out: list[tuple[str, list[Any]]] = []
    current, ids = head, list[Any]()
    for n, job in enumerate(jobs, first):
        block = _block(n, job)
        if current is not None and len(current) + 2 + len(block) > MAX_MESSAGE:
            out.append((current, ids))
            current, ids = None, []
        current = block if current is None else current + "\n\n" + block
        ids.append(job.get("id"))
    return [*out, *([(current, ids)] if current is not None else [])]


def format_digest(jobs: Sequence[Mapping[str, Any]], day: str) -> list[str]:
    return [text for text, _ in _messages(jobs, f"<b>Top {len(jobs)} new jobs</b> · {day}")]


def send_digest(db: Database, run_id: int, sender: Sender | None, *, limit: int) -> dict[str, Any]:
    """Send this run's digest unless it went already. A failed send frees the run again;
    a retry resumes after the last message that went out."""
    if sender is None:
        log.info("digest skipped: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not both set")
        return {"sent": 0, "skipped": "Telegram not configured"}
    if not db.claim_digest(run_id):
        return {"sent": 0, "skipped": f"already sent for run {run_id}"}
    sent = 0
    try:
        chunks, done = db.digest_progress(run_id)
        jobs = db.digest_jobs(max(limit - done, 0))
        day = datetime.now(IST).strftime("%d-%m-%Y")
        head = None if chunks else f"<b>Top {len(jobs)} new jobs</b> · {day}"
        for text, ids in _messages(jobs, head, done + 1) if jobs else []:
            sender.send(text)
            db.digest_chunk_sent(run_id, ids)
            sent += len(ids)
    except Exception as exc:
        db.release_digest(run_id)
        error = str(exc) if isinstance(exc, DigestError) else type(exc).__name__
        log.warning("digest for run %s not sent: %s", run_id, error)
        return {"sent": sent, "error": error}
    db.finish_digest(run_id)
    if not jobs:
        return {"sent": 0, "skipped": "no new ranked jobs"}
    log.info("digest for run %s sent: %s jobs", run_id, sent)
    return {"sent": sent}


def last_done_run(db: Database) -> int | None:
    """The latest finished discovery run (the runs table lives in the same database)."""
    try:
        row = db.conn.execute(
            "SELECT id FROM runs WHERE status='done' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except Exception:  # no runs table: nothing has ever run here
        return None
    return int(row["id"]) if row else None


def digest_last_run(db: Database, sender: Sender | None, *, limit: int) -> dict[str, Any]:
    """For the CLI and the cron endpoint: the digest of the last finished run."""
    if sender is None:
        return send_digest(db, 0, None, limit=limit)
    run_id = last_done_run(db)
    if run_id is None:
        return {"sent": 0, "skipped": "no finished run yet"}
    return {"run_id": run_id, **send_digest(db, run_id, sender, limit=limit)}
