"""Gmail for outreach: send one email on an owner click, and read thread headers to spot
replies. Scopes are gmail.send and gmail.metadata only: the app can never read a message body."""

from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPES = (
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.metadata",
)
ENV = ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")
BOUNCE_FROM = ("mailer-daemon", "postmaster")
Reply = Literal["none", "replied", "bounced"]


class GmailError(Exception):
    """Google refused or could not be reached. The message never holds a token."""


@dataclass(frozen=True)
class GmailConfig:
    client_id: str
    client_secret: str
    refresh_token: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> GmailConfig | None:
        values = [env.get(name, "").strip() for name in ENV]
        return cls(*values) if all(values) else None


@dataclass(frozen=True)
class Sent:
    message_id: str  # Gmail's id
    thread_id: str
    header_id: str  # the RFC 822 Message-ID, for In-Reply-To on follow-ups


def build_message(to: str, subject: str, body: str, *, in_reply_to: str = "") -> EmailMessage:
    """Plain text only: no tracking pixel, no links rewritten, no attachment."""
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    return msg


def _headers(payload: Mapping[str, Any]) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in payload.get("headers") or []}


class Gmail:
    def __init__(
        self, cfg: GmailConfig, *, transport: httpx.BaseTransport | None = None,
        timeout_s: float = 20.0, clock: Callable[[], float] = time.monotonic,
    ) -> None:  # fmt: skip
        self._cfg, self._clock = cfg, clock
        self._client = httpx.Client(timeout=timeout_s, transport=transport)
        self._token, self._until = "", 0.0

    def _call(self, method: str, url: str, **kw: Any) -> Any:
        try:
            res = self._client.request(method, url, **kw)
        except httpx.HTTPError as exc:
            raise GmailError(f"Google unreachable: {type(exc).__name__}") from None
        if res.status_code >= 400:
            try:
                why = str((res.json().get("error") or {}).get("message", ""))
            except (ValueError, AttributeError):
                why = ""
            raise GmailError(f"Google HTTP {res.status_code}: {why[:200]}")
        return res.json()

    def _access(self) -> str:
        if self._token and self._clock() < self._until:
            return self._token
        data = self._call("POST", TOKEN_URL, data={
            "grant_type": "refresh_token", "refresh_token": self._cfg.refresh_token,
            "client_id": self._cfg.client_id, "client_secret": self._cfg.client_secret,
        })  # fmt: skip
        self._token = str(data["access_token"])
        self._until = self._clock() + float(data.get("expires_in", 3600)) - 60
        return self._token

    def _api(self, method: str, path: str, **kw: Any) -> Any:
        auth = {"Authorization": f"Bearer {self._access()}"}
        return self._call(method, f"{API}{path}", headers=auth, **kw)

    def send(self, msg: EmailMessage, thread_id: str = "") -> Sent:
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        body = {"raw": raw, **({"threadId": thread_id} if thread_id else {})}
        sent = self._api("POST", "/messages/send", json=body)
        query = {"format": "metadata", "metadataHeaders": "Message-ID"}
        meta = self._api("GET", f"/messages/{sent['id']}", params=query)
        header_id = _headers(meta.get("payload") or {}).get("message-id", "")
        return Sent(str(sent["id"]), str(sent["threadId"]), header_id)

    def reply_state(self, thread_id: str) -> Reply:
        """A message in the thread that we did not send is a reply, or a bounce notice."""
        thread = self._api("GET", f"/threads/{thread_id}",
                           params={"format": "metadata", "metadataHeaders": "From"})  # fmt: skip
        state: Reply = "none"
        for m in thread.get("messages") or []:
            if "SENT" in (m.get("labelIds") or []):
                continue
            sender = _headers(m.get("payload") or {}).get("from", "").lower()
            if any(b in sender for b in BOUNCE_FROM):
                state = "bounced"
            else:
                return "replied"
        return state


def authorize(client_id: str, client_secret: str, *, open_url: Callable[[str], object],
              port: int = 0, timeout_s: float = 300.0) -> str:  # fmt: skip
    """One-time sign-in on the owner's own computer; returns the refresh token to store as
    the GMAIL_REFRESH_TOKEN secret. Uses a loopback redirect and PKCE."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    state, got = secrets.token_urlsafe(16), {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            query = parse_qs(urlsplit(self.path).query)
            if query.get("state", [""])[0] == state:
                got["code"] = query.get("code", [""])[0]
                got["error"] = query.get("error", [""])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Done. You can close this tab and go back to the terminal.")

        def log_message(self, *args: object) -> None:  # keep the code out of the terminal
            return

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    open_url(AUTH_URL + "?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge.decode().rstrip("="), "code_challenge_method": "S256",
    }))  # fmt: skip
    worker = threading.Thread(target=server.handle_request, daemon=True)
    server.timeout = timeout_s
    worker.start()
    worker.join(timeout_s)
    server.server_close()
    if not got.get("code"):
        raise GmailError(f"sign-in did not finish: {got.get('error') or 'no answer'}")
    res = httpx.post(TOKEN_URL, data={
        "grant_type": "authorization_code", "code": got["code"], "redirect_uri": redirect,
        "client_id": client_id, "client_secret": client_secret, "code_verifier": verifier,
    }, timeout=20)  # fmt: skip
    if res.status_code != 200 or "refresh_token" not in res.json():
        raise GmailError(f"Google did not return a refresh token (HTTP {res.status_code})")
    return str(res.json()["refresh_token"])
