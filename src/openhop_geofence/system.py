"""Operating system integration: systemd notification, clock trust, and reboot.

Everything here uses the standard library only.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import shutil
import socket
import subprocess

log = logging.getLogger(__name__)

#: Exit status that asks systemd to restart the service (``RestartForceExitStatus=``).
RESTART_EXIT_STATUS = 75


# ---------------------------------------------------------------------------
# systemd notification protocol (sd_notify) over a Unix datagram socket
# ---------------------------------------------------------------------------


class Notifier:
    """Minimal sd_notify implementation. A no-op outside systemd."""

    def __init__(self) -> None:
        self.address = os.environ.get("NOTIFY_SOCKET")
        self._sock: socket.socket | None = None
        if self.address:
            if self.address.startswith("@"):
                # Abstract namespace socket.
                self.address = "\0" + self.address[1:]
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)

    @property
    def under_systemd(self) -> bool:
        return self._sock is not None

    def notify(self, message: str) -> None:
        if not self._sock:
            return
        try:
            self._sock.sendto(message.encode(), self.address)
        except OSError as exc:
            log.debug("sd_notify failed: %s", exc)

    def ready(self) -> None:
        self.notify("READY=1")

    def watchdog(self) -> None:
        self.notify("WATCHDOG=1")

    def stopping(self) -> None:
        self.notify("STOPPING=1")

    def status(self, text: str) -> None:
        self.notify("STATUS=" + text.replace("\n", " ")[:200])


# ---------------------------------------------------------------------------
# Trusted wall-clock time
# ---------------------------------------------------------------------------

_STA_UNSYNC = 0x0040
_MAXERROR_LIMIT_US = 16_000_000  # Matches systemd's NTPSynchronized test.


class _Timex(ctypes.Structure):
    # Leading fields of ``struct timex`` (see adjtimex(2)); the remainder is padding.
    _fields_ = [
        ("modes", ctypes.c_uint),
        ("offset", ctypes.c_long),
        ("freq", ctypes.c_long),
        ("maxerror", ctypes.c_long),
        ("esterror", ctypes.c_long),
        ("status", ctypes.c_int),
        ("_reserved", ctypes.c_byte * 256),
    ]


_libc = None


def clock_synchronized() -> bool:
    """Return True if the kernel reports the system clock as synchronized.

    This is the condition ``timedatectl`` reports as ``NTPSynchronized``; it
    is set by systemd-timesyncd or chrony. ``adjtimex`` with ``modes = 0`` is
    read-only and needs no privileges.
    """
    global _libc
    try:
        if _libc is None:
            _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        tx = _Timex()
        if _libc.adjtimex(ctypes.byref(tx)) < 0:
            return False
    except (OSError, AttributeError):
        return False
    return not (tx.status & _STA_UNSYNC) and tx.maxerror < _MAXERROR_LIMIT_US


# ---------------------------------------------------------------------------
# Host reboot through systemd-logind (authorized by polkit)
# ---------------------------------------------------------------------------


def request_reboot() -> tuple[bool, str]:
    """Ask systemd-logind for an orderly reboot. Returns (accepted, message).

    ``systemctl reboot`` run by an unprivileged user calls logind, which
    consults polkit. Shutdown inhibitors are honored (no ``--force`` and no
    ``--check-inhibitors=no``).
    """
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False, "systemctl is not available; reboot unsupported on this host"
    try:
        proc = subprocess.run(
            [systemctl, "reboot", "--no-wall", "--no-ask-password"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"reboot request failed: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        return False, "reboot denied: " + (detail[-1] if detail else f"exit {proc.returncode}")
    return True, "reboot scheduled"
