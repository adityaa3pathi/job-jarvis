"""Run lock holding the pid, so two passes never overlap and stale locks are detected."""

from __future__ import annotations

import os
from pathlib import Path


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
    def __init__(self, path: Path, pid: int | None = None) -> None:
        self.path = path
        self.pid = pid if pid is not None else os.getpid()

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                holder = int(self.path.read_text().strip())
            except ValueError:
                holder = -1
            if holder > 0 and _alive(holder):
                raise LockHeldError(f"another run holds the lock (pid {holder})")
            self.path.unlink(missing_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(str(self.pid))

    def release(self) -> None:
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> RunLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
