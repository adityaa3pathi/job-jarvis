"""Portal policy table: the one place that decides whether an action may run (KTD3, KTD12)."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, field_validator

Mode = Literal["apply_automatically", "discover_only", "off"]
Action = Literal["discover", "apply", "submit"]
STOP_READ_TRIES = 3  # Turso reads fail under load; a run's first check tries this often
STOP_RETRY_WAIT_S = 0.5
log = logging.getLogger(__name__)


class PortalRule(BaseModel):
    mode: Mode = "off"
    live: bool = False
    discover: bool = False
    per_run_limit: int = 0
    min_pause_s: float = 20.0
    discover_pause_s: float = 1.0
    notes: str = ""

    @field_validator("mode", mode="before")
    @classmethod
    def _yaml_off(cls, value: object) -> object:
        # An unquoted `off` in YAML parses as False; read it as the intended "off".
        return "off" if value is False else value


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


class PolicyDenied(Exception):
    pass


def engage_kill_switch(stop_file: Path) -> None:
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.touch(mode=0o600)


def clear_kill_switch(stop_file: Path) -> None:
    stop_file.unlink(missing_ok=True)


@dataclass
class Policy:
    portals: dict[str, PortalRule]
    stop_file: Path
    dry_run: bool = True
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    # The durable kill switch (a DB flag); the STOP file stays a local trigger too.
    stop_check: Callable[[], bool] | None = None
    stop_cache_s: float = 5.0
    _last: dict[tuple[str, str], float] = field(default_factory=dict)
    _applied: dict[str, int] = field(default_factory=dict)
    stop_unread: bool = False  # discovery went ahead without a kill switch reading
    _stop_at: float | None = None
    _stop_known: bool | None = None
    _stop_lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def load(cls, path: Path, stop_file: Path, **kwargs: object) -> Policy:
        raw = yaml.safe_load(path.read_text()) or {}
        rows = {name.lower(): PortalRule.model_validate(v) for name, v in raw["portals"].items()}
        dry = bool(raw.get("global", {}).get("dry_run", True))
        return cls(portals=rows, stop_file=stop_file, dry_run=dry, **kwargs)  # type: ignore[arg-type]

    def rule(self, portal: str) -> PortalRule:
        return self.portals.get(portal.lower(), PortalRule())

    def stopped(self, action: Action = "submit") -> bool:
        """A failed DB read keeps the last known value. Never read at all: discovery runs
        (and the run is told), while applying and submitting stay halted."""
        if self.stop_file.exists():
            return True
        if self.stop_check is None:
            return False
        with self._stop_lock:  # portal threads share one cached read
            now = self.clock()
            if self._stop_at is None or now - self._stop_at >= self.stop_cache_s:
                first = self._stop_at is None
                self._stop_at = now
                value = self._read_stop(STOP_READ_TRIES if first else 1)
                self._stop_known = self._stop_known if value is None else value
            if self._stop_known is None:
                self.stop_unread = True
                return action != "discover"
            return self._stop_known

    def _read_stop(self, tries: int) -> bool | None:
        assert self.stop_check is not None
        error: Exception | None = None
        for attempt in range(tries):
            if attempt:
                self.sleep(STOP_RETRY_WAIT_S)
            try:
                return bool(self.stop_check())
            except Exception as exc:
                error = exc
        log.error("kill switch could not be read (%d tries): %s", tries, error)
        return None

    def authorize(self, portal: str, action: Action, *, live: bool = False) -> Decision:
        """Pure check. Call again right before the Submit click, STOP may appear in between."""
        if self.stopped(action):
            return Decision(False, "kill switch is on")
        rule = self.rule(portal)
        if portal.lower() not in self.portals or rule.mode == "off":
            return Decision(False, f"portal {portal!r} is off")
        if action == "discover":
            if not rule.discover:
                return Decision(False, f"discovery is not enabled for {portal!r}")
            return Decision(True, "discovery allowed")
        if rule.mode != "apply_automatically":
            return Decision(False, f"portal {portal!r} is {rule.mode}, not apply_automatically")
        if action == "apply":
            return Decision(True, "dry-run apply allowed")
        if self.dry_run or not live or not rule.live:
            return Decision(False, "live submit needs global live, --live and portal live: true")
        return Decision(True, "live submit allowed")

    def begin(self, portal: str, action: Action, *, live: bool = False) -> Decision:
        """Authorize, enforce the per-run limit and pause, then check STOP again."""
        decision = self.authorize(portal, action, live=live)
        if not decision.allowed:
            return decision
        key = portal.lower()
        rule = self.rule(key)
        if action != "discover" and self._applied.get(key, 0) >= rule.per_run_limit:
            return Decision(False, f"per-run limit {rule.per_run_limit} reached for {portal!r}")
        pause = rule.discover_pause_s if action == "discover" else rule.min_pause_s
        last = self._last.get((key, action))
        if last is not None:
            wait = last + pause - self.clock()
            if wait > 0:
                self.sleep(wait)
        if self.stopped(action):
            return Decision(False, "kill switch is on")
        self._last[(key, action)] = self.clock()
        if action == "apply":
            self._applied[key] = self._applied.get(key, 0) + 1
        return decision

    def require(self, portal: str, action: Action, *, live: bool = False) -> None:
        decision = self.begin(portal, action, live=live)
        if not decision.allowed:
            raise PolicyDenied(decision.reason)
