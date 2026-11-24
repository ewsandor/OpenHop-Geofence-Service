"""Durable and runtime state with minimal storage wear.

Persistent writes are event-driven and atomic: write a temporary file in the
destination directory, fsync it, rename it over the target, then fsync the
directory. Runtime files live in the RAM-backed runtime directory (``/run``).
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SAFE_MODE_FILE = "safe-mode.json"


def atomic_write(path: str | Path, data: bytes, mode: int | None = None, fsync: bool = True) -> None:
    """Atomically replace ``path`` with ``data``.

    ``mode`` defaults to the existing file's permissions, or 0o640. Ownership
    of an existing file is kept; a new file takes the directory's group (so a
    root-run ``passwd`` leaves the file readable by the service group). With
    ``fsync=False`` (used on tmpfs) the rename is still atomic but not synced.
    """
    path = Path(path)
    try:
        st = path.stat()
        uid, gid = st.st_uid, st.st_gid
        if mode is None:
            mode = st.st_mode & 0o7777
    except FileNotFoundError:
        uid, gid = -1, path.parent.stat().st_gid
        if mode is None:
            mode = 0o640
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())
        try:
            os.chown(tmp, uid, gid)
        except PermissionError:
            pass  # Unprivileged writers keep their own user and group.
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    if fsync:
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


@dataclass(frozen=True)
class SafeModeLatch:
    """The persistent safe mode latch. Requires OpenHop ``no_tx`` while active."""

    active: bool = False
    cause: str = ""
    since_utc: float | None = None


class SafeModeStore:
    """Reads and writes the safe mode latch in the durable state directory."""

    def __init__(self, state_dir: str | Path):
        self.path = Path(state_dir) / SAFE_MODE_FILE

    def load(self) -> SafeModeLatch:
        """Load the latch. A missing file means inactive.

        An unreadable or corrupt latch is treated as *active*: the service
        cannot prove that safe mode was cleared.
        """
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return SafeModeLatch()
        except (OSError, ValueError) as exc:
            return SafeModeLatch(True, f"unreadable safe mode latch: {exc}", None)
        if not isinstance(data, dict) or not isinstance(data.get("active"), bool):
            return SafeModeLatch(True, "corrupt safe mode latch", None)
        since = data.get("since_utc")
        return SafeModeLatch(
            data["active"], str(data.get("cause", "")), since if isinstance(since, (int, float)) else None
        )

    def save(self, latch: SafeModeLatch) -> None:
        """Persist the latch durably. Raises OSError on failure."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, json.dumps(asdict(latch)).encode(), mode=0o640)


class RuntimeDir:
    """The RAM-backed runtime directory: lock, status, and request accounting.

    Contents survive service restarts (``RuntimeDirectoryPreserve=yes``) but
    not reboots. Writes here never reach persistent storage.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock_fd: int | None = None

    def acquire_lock(self) -> None:
        """Take an exclusive lock so only one instance runs. Raises RuntimeError."""
        self.path.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path / "lock", os.O_RDWR | os.O_CREAT, 0o640)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise RuntimeError(f"another instance holds {self.path / 'lock'}") from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._lock_fd = fd

    def release_lock(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def read_json(self, name: str) -> dict[str, Any] | None:
        try:
            data = json.loads((self.path / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def write_json(self, name: str, data: dict[str, Any]) -> None:
        """Write a JSON file to the runtime directory (no fsync on tmpfs)."""
        self.path.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path / name, json.dumps(data, default=str).encode(), mode=0o640, fsync=False)


def utc_now() -> float:
    """Wall-clock time; trusted only when the system clock is synchronized."""
    return time.time()
