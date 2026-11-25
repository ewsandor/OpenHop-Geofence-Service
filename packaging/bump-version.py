#!/usr/bin/env python3
"""Update the Python, Debian, and RPM versions and release changelogs."""

import datetime
import email.utils
import re
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z", re.ASCII)


def prompt(label: str, default: str = "") -> str:
    value = input(f"{label}{f' [{default}]' if default else ''}: ").strip() or default
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{label} must be a non-empty single line")
    return value


def replace_once(text: str, pattern: str, replacement: str) -> str:
    result, count = re.subn(pattern, lambda _: replacement, text, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"expected exactly one match for {pattern!r}, found {count}")
    return result


def main() -> None:
    subprocess.run(["sh", "packaging/check-version.sh"], cwd=ROOT, check=True)
    python_path = ROOT / "src/openhop_geofence/__init__.py"
    deb_path = ROOT / "debian/changelog"
    spec_path = ROOT / "packaging/fedora/openhop-geofence.spec"
    python_text = python_path.read_text(encoding="utf-8")
    deb_text = deb_path.read_text(encoding="utf-8")
    spec_text = spec_path.read_text(encoding="utf-8")
    control = (ROOT / "debian/control").read_text(encoding="utf-8")

    current_match = re.search(r'^__version__ = "([^"]+)"$', python_text, re.MULTILINE)
    maintainer = re.search(r"^Maintainer: (.+ <[^<>@\s]+@[^<>@\s]+>)$", control, re.MULTILINE)
    if not current_match or not VERSION.fullmatch(current_match.group(1)) or not maintainer:
        raise ValueError("could not read current version or Debian maintainer")
    current = current_match.group(1)
    version = prompt("New version (MAJOR.MINOR.PATCH)")
    if not VERSION.fullmatch(version) or tuple(map(int, version.split("."))) <= tuple(map(int, current.split("."))):
        raise ValueError(f"version must be MAJOR.MINOR.PATCH and greater than {current}")
    summary = prompt("Release summary")
    author = prompt("Changelog author (Name <email>)", maintainer.group(1))
    if not re.fullmatch(r"[^<>]+ <[^<>@\s]+@[^<>@\s]+>", author):
        raise ValueError("changelog author must be in the form Name <email>")

    now = datetime.datetime.now().astimezone()
    deb_date = email.utils.format_datetime(now)
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    rpm_date = f"{weekdays[now.weekday()]} {months[now.month - 1]} {now.day:02} {now.year}"
    deb_summary = textwrap.fill(summary, width=78, initial_indent="  * ", subsequent_indent="    ",
                                break_long_words=False, break_on_hyphens=False)
    rpm_summary = textwrap.fill(summary.replace("%", "%%"), width=78,
                                initial_indent="- ", subsequent_indent="  ",
                                break_long_words=False, break_on_hyphens=False)
    deb_entry = (f"openhop-geofence ({version}) unstable; urgency=medium\n\n"
                 f"{deb_summary}\n\n -- {author}  {deb_date}\n\n")
    rpm_entry = f"%changelog\n* {rpm_date} {author.replace('%', '%%')} - {version}-1\n{rpm_summary}\n"
    python_new = replace_once(python_text, r'^__version__ = "[^"]+"$', f'__version__ = "{version}"')
    spec_new = replace_once(spec_text, r"^Version:[ \t]*[^\n]+$", f"Version:        {version}")
    spec_new = replace_once(spec_new, r"^Release:[ \t]*[^\n]+$", "Release:        1%{?dist}")
    spec_new = replace_once(spec_new, r"^%changelog$", rpm_entry.rstrip("\n"))

    python_path.write_text(python_new, encoding="utf-8")
    deb_path.write_text(deb_entry + deb_text, encoding="utf-8")
    spec_path.write_text(spec_new, encoding="utf-8")
    subprocess.run(["sh", "packaging/check-version.sh", f"v{version}"], cwd=ROOT, check=True)
    print(f"Updated to {version}. Review the changes; no commit or tag was created.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, EOFError, subprocess.CalledProcessError) as exc:
        print(f"Version bump failed: {exc}", file=sys.stderr)
        sys.exit(1)
