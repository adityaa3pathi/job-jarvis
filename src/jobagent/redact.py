"""Removes secrets and URL query strings from any text before it is logged or sent."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable

_PATTERNS = [
    re.compile(r"bot[0-9]+:[A-Za-z0-9_-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:ghp|gho|xox[bap])[-_][A-Za-z0-9_-]{16,}"),
    re.compile(r"\b[A-Za-z0-9_-]{32,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}"),
]
_URL_QUERY = re.compile(r"(https?://[^\s?#\"'<>]+)\?[^\s\"'<>]*")
_SECRET_NAME = re.compile(r"(TOKEN|KEY|SECRET|PASSWORD|CHAT_ID)", re.IGNORECASE)
MASK = "[REDACTED]"


class Redactor:
    def __init__(self, secret_values: Iterable[str] = ()) -> None:
        self._values = {v for v in secret_values if len(v) >= 4}

    @classmethod
    def from_environment(cls, extra: Iterable[str] = ()) -> Redactor:
        env = [v for k, v in os.environ.items() if _SECRET_NAME.search(k) and v]
        return cls([*env, *extra])

    def __call__(self, text: str) -> str:
        # Longest values first so a value that contains another is masked whole.
        for value in sorted(self._values, key=len, reverse=True):
            text = text.replace(value, MASK)
        for pattern in _PATTERNS:
            text = pattern.sub(MASK, text)
        return _URL_QUERY.sub(r"\1", text)


class RedactingFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor, fmt: str | None = None) -> None:
        super().__init__(fmt or "%(asctime)s %(levelname)s %(name)s: %(message)s")
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self._redactor(super().format(record))


def configure_logging(redactor: Redactor, level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(redactor))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # HTTP client loggers print full URLs, so keep them quiet.
    for name in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
