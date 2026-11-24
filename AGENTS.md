# AGENTS.md

Guidance for AI assistants and contributors working on this repository.
[DESIGN.md](DESIGN.md) is the authoritative specification; read the relevant
section before changing behavior. Update DESIGN.md and README.md when behavior,
configuration, or packaging changes.

## Safety invariants

Do not break these without an explicit design change:

1. The controller (`controller.py`) is the only component that writes the
   OpenHop mode. The web, companion, and trace modules read status snapshots
   and submit commands to the controller queue.
2. Decision priority (`policy.py`): safe mode > manual override > missing or
   stale GPS (Monitor) > Monitor zone > Repeat zone > MeshMapper > `policy.outside`.
3. Safe mode is latched persistently and requests `no_tx`. `resume` never
   clears it; only `clear` does, after health checks. GPS loss never releases
   an override or safe mode.
4. Read the OpenHop mode before every write and confirm it by readback. HTTP
   200 is not success. Never report a requested mode as the observed mode.
   Do not write a mode that is already observed.
5. Timers for hysteresis, staleness, and grace periods use monotonic time.
   Wall-clock values (MeshMapper age, companion freshness, trace UTC) are used
   only when the kernel reports clock synchronization.
6. MeshMapper requests respect the daily budget, minimum spacing, and backoff
   (`meshmapper.py`). Tests must never contact MeshMapper or a live radio.
7. No steady-state writes to persistent storage. Persistent files are written
   only for state changes, configuration edits, and coverage refreshes, using
   `state.atomic_write`. Runtime files go in the runtime directory (tmpfs).
8. Optional dependencies (Shapely, Flask, Waitress, tomlkit, meshcore) are
   imported lazily, only when their feature is enabled. A minimal install
   with only the standard library must work with circle zones.
9. Secrets are read from files, never logged, exported, or shown in the web
   interface.
10. Do not add dependencies outside the standard library without a design
    change; never vendor dependencies.

## Architecture

| Module | Responsibility |
| --- | --- |
| `cli.py` | Argument parsing, wiring, signals, exit codes (75 restart, 78 invalid config), `passwd` |
| `config.py` | TOML loading and validation into frozen dataclasses |
| `policy.py` | Pure decision function and hysteresis |
| `controller.py` | Main loop, command queue, OpenHop writes, safe mode, status snapshots |
| `zones.py` | Circle (haversine) and GeoJSON polygon (Shapely) zones |
| `gpsd.py` | gpsd JSON client thread |
| `openhop.py` | OpenHop HTTP API client (`urllib`) |
| `meshmapper.py` | Coverage download, cache, quota accounting, square lookup |
| `state.py` | Atomic writes and persisted latches |
| `system.py` | sd_notify, clock synchronization check, reboot request |
| `web.py`, `templates/`, `static/` | Optional Flask web interface |
| `companion.py` | Optional MeshCore companion (asyncio thread) |
| `trace.py`, `trace_convert.py` | Optional diagnostic trace and offline GeoJSON converter |

## Validation

```sh
# Unit and integration tests (fake gpsd/OpenHop/MeshMapper servers)
PYTHONPATH=src:tests python3 -W error::ResourceWarning -m unittest discover -s tests

# Lint (as in CI)
ruff check --select F,E9,B src/ tests/

# Example configuration
PYTHONPATH=src python3 -m openhop_geofence --check-config --config config/config.toml.example

# Version consistency across __init__.py, debian/changelog, and the RPM spec
sh packaging/check-version.sh
```

Web and polygon tests are skipped when Flask or Shapely is not installed.
Use a virtual environment with `--system-site-packages` for optional
dependencies; do not use pip on Debian's system Python.

`tests/fakes.py` provides fake gpsd, OpenHop, and MeshMapper servers and a
controllable clock. Add tests for every behavior change; prefer the fakes over
mocks for network behavior.

## Packaging

- Version: `src/openhop_geofence/__init__.py` (`__version__`) is the single
  source. A release also needs a `debian/changelog` entry and a matching
  `Version:` and `%changelog` entry in `packaging/fedora/openhop-geofence.spec`.
- Debian: `debian/` (native, `Architecture: all`, pybuild). Build with
  `dpkg-buildpackage -us -uc -b`.
- Fedora: `packaging/fedora/openhop-geofence.spec`.
- systemd: `packaging/openhop-geofence.service` (used by both packages and by
  `install-systemd.sh`).
- CI: `.github/workflows/ci.yml` (pull requests) and `release.yml` (`v*` tags).

## Troubleshooting

- Run in the foreground with `--log-level DEBUG` and local `--state-dir`,
  `--cache-dir`, and `--runtime-dir` paths.
- The status endpoint (`/api/status`) and `debug` companion command show
  controller state, fault reasons, and worker errors.
- Use the diagnostic trace and `openhop-geofence-trace --summary` for mode
  transitions over a drive.
