"""Command-line entry point and process wiring.

``openhop-geofence`` runs the service in the foreground (standalone or under
systemd). Subcommands and options:

* ``--check-config``: validate the configuration and exit.
* ``passwd USER``: create or update a web interface account.

Under systemd the unit provides ``STATE_DIRECTORY``, ``CACHE_DIRECTORY``,
and ``RUNTIME_DIRECTORY``; standalone runs may pass ``--state-dir`` etc.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import signal
import sys
import tomllib
from pathlib import Path

from . import __version__
from . import config as config_mod
from .config import ConfigError
from .state import RuntimeDir, SafeModeLatch, SafeModeStore, utc_now
from .system import RESTART_EXIT_STATUS

log = logging.getLogger("openhop_geofence")

DEFAULT_CONFIG = "/etc/openhop-geofence/config.toml"
#: Invalid configuration at startup; the unit sets ``RestartPreventExitStatus=78``
#: so systemd does not restart in a loop until the file is fixed.
CONFIG_EXIT_STATUS = 78


def _first_dir(env: str, default: str) -> str:
    """systemd may pass several colon-separated directories; use the first."""
    value = os.environ.get(env)
    return value.split(":")[0] if value else default


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="openhop-geofence",
        description="Geofence an OpenHop MeshCore repeater between Repeat and Monitor modes.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-c", "--config", default=os.environ.get("OPENHOP_GEOFENCE_CONFIG", DEFAULT_CONFIG),
                   help=f"configuration file (default {DEFAULT_CONFIG})")
    p.add_argument("--check-config", action="store_true", help="validate the configuration and exit")
    p.add_argument("--state-dir", default=_first_dir("STATE_DIRECTORY", "/var/lib/openhop-geofence"),
                   help="durable state (safe mode latch)")
    p.add_argument("--cache-dir", default=_first_dir("CACHE_DIRECTORY", "/var/cache/openhop-geofence"),
                   help="MeshMapper coverage snapshot")
    p.add_argument("--runtime-dir", default=_first_dir("RUNTIME_DIRECTORY", "/run/openhop-geofence"),
                   help="RAM-backed runtime files (lock, request accounting)")
    p.add_argument("--log-level", default=os.environ.get("OPENHOP_GEOFENCE_LOG_LEVEL", "INFO"),
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    sub = p.add_subparsers(dest="command")
    pw = sub.add_parser("passwd", help="create or update a web interface account")
    pw.add_argument("user")
    pw.add_argument("--role", choices=["admin", "operator"], default="admin")
    pw.add_argument("--auth-file", help="account file (default: web.auth_file from the configuration)")
    return p


def setup_logging(level: str) -> None:
    # journald adds timestamps; standalone terminals get them from the formatter.
    if os.environ.get("JOURNAL_STREAM"):
        fmt = "%(levelname)s %(name)s: %(message)s"
    else:
        fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=getattr(logging, level), format=fmt, stream=sys.stderr)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def check_config(path: str) -> int:
    try:
        cfg = config_mod.load_config(path)
    except (OSError, ConfigError) as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        return 1
    print(f"{path}: OK (revision {cfg.revision}, {len(cfg.zones)} zone(s))")
    for w in cfg.warnings:
        print(f"warning: {w}")
    if cfg.web.enabled:
        from .web import ensure_auth_file_warning, web_available

        ok, why = web_available()
        if not ok:
            print(f"warning: {why}")
        elif warn := ensure_auth_file_warning(cfg.web.auth_file):
            print(f"warning: {warn}")
    if cfg.companion.enabled:
        try:
            import meshcore  # noqa: F401
        except ImportError:
            print("warning: companion enabled but the meshcore package is not installed")
    return 0


def passwd(args: argparse.Namespace) -> int:
    from .web import close_initial_setup, set_password

    auth_file = args.auth_file
    if not auth_file:
        try:
            auth_file = config_mod.load_config(args.config).web.auth_file
        except (OSError, ConfigError) as exc:
            print(f"cannot read {args.config}: {exc}; use --auth-file", file=sys.stderr)
            return 1
    pw1 = getpass.getpass(f"New password for {args.user}: ")
    if len(pw1) < 8:
        print("password must be at least 8 characters", file=sys.stderr)
        return 1
    if getpass.getpass("Repeat password: ") != pw1:
        print("passwords do not match", file=sys.stderr)
        return 1
    try:
        try:
            configured_auth = config_mod.load_config(args.config).web.auth_file
        except (OSError, ConfigError):
            configured_auth = None
        if configured_auth and Path(auth_file).absolute() == Path(configured_auth).absolute():
            close_initial_setup(Path(args.state_dir))
        set_password(auth_file, args.user, args.role, pw1)
    except (OSError, ValueError) as exc:
        print(f"cannot update {auth_file}: {exc}", file=sys.stderr)
        return 1
    print(f"{args.user} ({args.role}) saved to {auth_file}")
    return 0


# ---------------------------------------------------------------------------
# Fail-safe for faults outside the controller
# ---------------------------------------------------------------------------


def fail_safe(config_path: str, state_dir: str, cause: str) -> None:
    """Latch safe mode and attempt OpenHop ``no_tx`` without a valid configuration.

    Used when the configuration is invalid at startup or the controller dies
    unexpectedly. The OpenHop URL and key are read from the raw file when
    possible, otherwise the defaults are used.
    """
    from .openhop import OpenHopClient, OpenHopError

    log.critical("%s; entering safe mode", cause)
    try:
        SafeModeStore(state_dir).save(SafeModeLatch(True, cause[:200], utc_now()))
    except OSError as exc:
        log.critical("cannot persist safe mode latch: %s", exc)
    oh = config_mod.OpenHopConfig()
    try:
        raw = tomllib.loads(Path(config_path).read_text(encoding="utf-8")).get("openhop", {})
        url = raw.get("base_url", oh.base_url)
        key_file = raw.get("api_key_file", oh.api_key_file)
    except (OSError, ValueError, AttributeError):
        url, key_file = oh.base_url, oh.api_key_file
    try:
        OpenHopClient(str(url), config_mod.read_secret(str(key_file)), oh.timeout_seconds).set_mode("no_tx")
        log.warning("requested OpenHop no_tx")
    except (OpenHopError, ValueError) as exc:
        log.critical("could not request OpenHop no_tx: %s", exc)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def run_service(args: argparse.Namespace) -> int:
    from .controller import Controller
    from .gpsd import GpsdReader
    from .openhop import OpenHopClient
    from .system import Notifier, clock_synchronized

    try:
        cfg = config_mod.load_config(args.config)
    except (OSError, ConfigError) as exc:
        fail_safe(args.config, args.state_dir, f"invalid configuration at startup: {str(exc).splitlines()[0]}")
        print(f"{args.config}: {exc}", file=sys.stderr)
        return CONFIG_EXIT_STATUS
    for w in cfg.warnings:
        log.warning("configuration: %s", w)

    runtime = RuntimeDir(args.runtime_dir)
    try:
        runtime.acquire_lock()
    except (RuntimeError, OSError) as exc:
        log.error("cannot start: %s", exc)
        return 1

    notifier = Notifier()
    gps = GpsdReader(cfg.gpsd.host, cfg.gpsd.port, cfg.gpsd.stale_after_seconds, cfg.gpsd.device,
                     time_trusted=clock_synchronized)
    gps.start()

    def make_openhop(c: config_mod.Config) -> OpenHopClient:
        return OpenHopClient(c.openhop.base_url, config_mod.read_secret(c.openhop.api_key_file),
                             c.openhop.timeout_seconds)

    controller: Controller

    def make_meshmapper(c: config_mod.Config):
        if not c.meshmapper.enabled:
            return None
        from .meshmapper import MeshMapperWorker

        return MeshMapperWorker(c.meshmapper, args.cache_dir, runtime,
                                radio_provider=lambda: controller.radio, time_trusted=clock_synchronized)

    controller = Controller(
        cfg, args.config, gps=gps, safe_store=SafeModeStore(args.state_dir),
        make_openhop=make_openhop, make_meshmapper=make_meshmapper, notifier=notifier,
        time_trusted=clock_synchronized,
    )

    # Optional modules. Each is imported only when enabled; a failure to start
    # one is reported but never stops mode control.
    trace = None
    if cfg.diagnostics.enabled:
        from .trace import TraceRecorder

        trace = TraceRecorder(
            cfg.diagnostics, time_trusted=clock_synchronized,
            coverage_lookup=lambda lat, lon: controller.meshmapper.squares_near(lat, lon) if controller.meshmapper else [],
            coverage_revision=lambda: controller.meshmapper.coverage_revision() if controller.meshmapper else None,
            spool_dir=args.runtime_dir,
        )

        def trace_hook(ev) -> None:
            trace.set_config(controller.cfg)  # Records zone context once per revision.
            trace.on_evaluation(ev)

        controller.evaluation_hooks.append(trace_hook)
        controller.listeners.append(trace)

    companion = None
    if cfg.companion.enabled:
        try:
            from .companion import Companion

            companion = Companion(cfg.companion, controller, time_trusted=clock_synchronized)
        except ImportError as exc:
            log.error("companion unavailable: %s is not installed", exc.name)
            controller.status_providers["companion"] = lambda: {"enabled": True, "error": "meshcore not installed"}
        else:
            controller.listeners.append(companion)
            controller.announce_failure = companion
            controller.status_providers["companion"] = companion.status
            companion.start()

    web = None
    if cfg.web.enabled:
        from .web import WebServer, create_app, ensure_auth_file_warning, web_available

        ok, why = web_available()
        if not ok:
            log.error(why)
        else:
            if warn := ensure_auth_file_warning(cfg.web.auth_file):
                log.warning(warn)
            web = WebServer(create_app(controller, trace=trace, setup_dir=Path(args.state_dir)), cfg.web.bind, cfg.web.port)
            if not web.start():
                why = web.error
        # Shown by the CLI status and logs; the browser cannot see a dead server.
        controller.status_providers["web_server"] = lambda: {"error": why or None}

    signal.signal(signal.SIGTERM, lambda *_: controller.stop())
    signal.signal(signal.SIGINT, lambda *_: controller.stop())
    signal.signal(signal.SIGHUP, lambda *_: controller.submit("reload", requester="SIGHUP"))

    try:
        status = controller.run()
    except Exception:
        log.exception("controller failed")
        fail_safe(args.config, args.state_dir, "unexpected controller failure")
        status = 1
    finally:
        if web:
            web.stop()
        if companion:
            companion.stop()
        if trace:
            trace.stop()
        gps.stop()
        runtime.release_lock()

    if status == RESTART_EXIT_STATUS and not notifier.under_systemd:
        # Standalone: replace this process with a fresh one.
        log.info("restarting")
        logging.shutdown()
        os.execv(sys.executable, [sys.executable, "-m", "openhop_geofence", *sys.argv[1:]])
    return status


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "passwd":
        return passwd(args)
    if args.check_config:
        return check_config(args.config)
    setup_logging(args.log_level)
    return run_service(args)


if __name__ == "__main__":
    sys.exit(main())
