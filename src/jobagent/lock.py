"""Run lock holding the pid and start time, so two passes never overlap and stale locks clear."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

STALE_AFTER_S = 30 * 60  # a daily run takes minutes; a lock older than this is left over


class LockHeldError(Exception):
    pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunLock:
    def __init__(
        self,
        path: Path,
        pid: int | None = None,
        max_age_s: float = STALE_AFTER_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self.pid = pid if pid is not None else os.getpid()
        self.max_age_s, self.clock = max_age_s, clock

    def _holder(self) -> tuple[int, float]:
        parts = self.path.read_text().split()
        try:
            pid = int(parts[0])
        except (IndexError, ValueError):
            pid = -1
        try:
            started = float(parts[1])
        except (IndexError, ValueError):  # older lock files hold only the pid
            started = self.path.stat().st_mtime
        return pid, started

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            holder, started = self._holder()
        except FileNotFoundError:
            pass
        else:
            # Containers reuse low pids, so a live pid alone does not prove a run holds it.
            fresh = self.clock() - started < self.max_age_s
            if holder > 0 and _alive(holder) and fresh:
                raise LockHeldError(f"another run holds the lock (pid {holder})")
            self.path.unlink(missing_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise LockHeldError("another run took the lock just now") from exc
        with os.fdopen(fd, "w") as handle:
            handle.write(f"{self.pid} {self.clock()}")

    def release(self) -> None:
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> RunLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
