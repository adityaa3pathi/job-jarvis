"""Append-only JSON-lines audit log and screenshot store (R14, KTD16)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jobagent.redact import Redactor


class AuditLog:
    def __init__(
        self,
        directory: Path,
        redactor: Redactor,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.directory = directory
        self.redactor = redactor
        self._now = now
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "audit.jsonl"

    def write(
        self,
        portal: str,
        job: str,
        action: str,
        result: str,
        detail: str = "",
        screenshot: Path | None = None,
    ) -> dict[str, Any]:
        entry = {
            "time": self._now().isoformat(),
            "portal": portal,
            "job": job,
            "action": action,
            "result": result,
            "detail": detail,
            "screenshot": str(screenshot) if screenshot else None,
        }
        line = self.redactor(json.dumps(entry, ensure_ascii=False))
        fd = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a") as handle:
            handle.write(line + "\n")
        parsed: dict[str, Any] = json.loads(line)
        return parsed

    def save_screenshot(self, png: bytes, name: str) -> Path:
        safe = "".join(c for c in name if c.isalnum() or c in "-_.")[:80] or "shot"
        path = self.directory / f"{self._now():%Y%m%dT%H%M%S}-{safe}.png"
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(png)
        return path

    def error(self, portal: str, job: str, action: str, exc: BaseException) -> dict[str, Any]:
        return self.write(portal, job, action, "error", detail=f"{type(exc).__name__}: {exc}")

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text().splitlines() if x.strip()]
