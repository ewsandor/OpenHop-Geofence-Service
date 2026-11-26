"""Optional web interface (Flask application served by Waitress).

The web interface reads controller status snapshots and submits commands; it
never writes the radio mode directly. Flask, Waitress, and tomlkit are
imported only when the interface is enabled.

Security (DESIGN.md section 9):

* APIs require login, including on localhost, except for one-time setup.
* Accounts are stored in ``web.auth_file`` as ``user:role:werkzeug-hash``
  lines (role ``admin`` or ``operator``); administrators can change their
  password in the web interface or use ``openhop-geofence passwd``.
* Login uses a password only. Sessions use signed cookies with a persistent
  service-owned key so remembered sessions survive service restarts.
* Mutations require POST, a CSRF token header, and a same-origin request.
  The Host header must be an IP literal, ``localhost``, or this host's name.
* Request bodies and login attempts are bounded.
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
import socket
import stat
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import __version__
from . import config as config_mod
from .controller import CommandError
from .state import managed_secret_path

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 512 * 1024
LOGIN_WINDOW_SECONDS = 300
LOGIN_MAX_FAILURES = 5
REMEMBER_DAYS = 30
ROLES = ("admin", "operator")
ACTIONS = {
    "force_repeat": ("override", {"mode": "forward"}),
    "force_monitor": ("override", {"mode": "monitor"}),
    "disable_tx": ("disable_tx", {}),
    "clear_tx_lockout": ("clear_tx_lockout", {}),
    "resume": ("resume", {}),
    "clear": ("clear", {}),
    "reload": ("reload", {}),
}


def web_available() -> tuple[bool, str]:
    """Return whether the optional web dependencies can be imported."""
    try:
        import flask  # noqa: F401
        import tomlkit  # noqa: F401
        import waitress  # noqa: F401
    except ImportError as exc:
        return False, f"web interface unavailable: {exc.name} is not installed"
    return True, ""


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def load_accounts(path: str) -> dict[str, tuple[str, str]]:
    """Return {user: (role, hash)}. Malformed lines are ignored with a warning."""
    accounts: dict[str, tuple[str, str]] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.warning("cannot read web auth file %s: %s", path, exc)
        return accounts
    for n, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        user, _, rest = line.partition(":")
        role, _, pwhash = rest.partition(":")
        if not user or role not in ROLES or not pwhash:
            log.warning("%s:%d: malformed account line ignored", path, n)
            continue
        accounts[user] = (role, pwhash)
    return accounts


def set_password(path: str, user: str, role: str, password: str) -> None:
    """Create or replace one account line (used by ``openhop-geofence passwd``)."""
    from werkzeug.security import generate_password_hash

    from .state import atomic_write

    if not user or ":" in user or role not in ROLES:
        raise ValueError("user must be non-empty without ':'; role must be admin or operator")
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    lines = [ln for ln in lines if ln.partition(":")[0] != user]
    lines.append(f"{user}:{role}:{generate_password_hash(password)}")
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(p, ("\n".join(lines) + "\n").encode(), mode=0o640)


def close_initial_setup(state_dir: Path) -> bool:
    """Claim the one-time setup marker, durably and without replacing a claim."""
    pending = state_dir / "web-setup-pending"
    claimed = state_dir / "web-setup-claimed"
    try:
        os.link(pending, claimed)
    except FileNotFoundError:
        return False
    except FileExistsError:
        return False
    pending.unlink()
    fd = os.open(state_dir, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def _session_signing_key(state_dir: Path | None) -> bytes:
    """Load or create a persistent Flask signing key when a state directory is configured."""
    if state_dir is None:
        return secrets.token_bytes(32)
    state_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    path = state_dir / "web-session.key"
    nofollow = getattr(os, "O_NOFOLLOW", 0)

    try:
        fd = os.open(path, os.O_RDONLY | nofollow)
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
        except FileExistsError:
            fd = os.open(path, os.O_RDONLY | nofollow)
        else:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                if stream.write(key) != len(key):
                    raise OSError("short write creating web session key")
                stream.flush()
                os.fsync(stream.fileno())
            dir_fd = os.open(state_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
            return key

    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        key = stream.read(33)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o777 != 0o600 or len(key) != 32):
        raise OSError(f"{path} must be a service-owned regular 32-byte file with mode 0600")
    return key


# ---------------------------------------------------------------------------
# Zone editing (tomlkit preserves comments and formatting)
# ---------------------------------------------------------------------------


def replace_zones(text: str, zones: list[dict]) -> str:
    """Return config text with the ``[[zones]]`` array replaced.

    Each zone is validated with the same parser as the configuration file.
    Raises ValueError for invalid zones.
    """
    import tomlkit

    if not isinstance(zones, list) or len(zones) > 500:
        raise ValueError("zones must be a list of at most 500 entries")
    for i, z in enumerate(zones):
        try:
            config_mod.parse_zone(z)
        except ValueError as exc:
            raise ValueError(f"zone {i + 1}: {exc}") from None
    doc = tomlkit.parse(text)
    if "zones" in doc:
        del doc["zones"]
    if zones:
        aot = tomlkit.aot()
        for z in zones:
            table = tomlkit.table()
            for key in ("id", "name", "role", "shape", "latitude", "longitude", "radius_m"):
                if key in z:
                    table[key] = z[key]
            if "geometry" in z:
                geom = tomlkit.inline_table()
                geom.update({"type": z["geometry"]["type"], "coordinates": z["geometry"]["coordinates"]})
                table["geometry"] = geom
            aot.append(table)
        doc["zones"] = aot
    return tomlkit.dumps(doc)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


class LoginThrottle:
    """Bounded per-address login failure tracking (in memory only)."""

    def __init__(self) -> None:
        self._failures: dict[str, collections.deque[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, addr: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._failures.get(addr)
            while q and now - q[0] > LOGIN_WINDOW_SECONDS:
                q.popleft()
            return bool(q) and len(q) >= LOGIN_MAX_FAILURES

    def failed(self, addr: str) -> None:
        with self._lock:
            if len(self._failures) > 1024:
                self._failures.clear()  # Bound memory under address churn.
            self._failures.setdefault(addr, collections.deque(maxlen=LOGIN_MAX_FAILURES)).append(time.monotonic())

    def succeeded(self, addr: str) -> None:
        with self._lock:
            self._failures.pop(addr, None)


def _allowed_host(host: str) -> bool:
    name = urlparse("//" + host).hostname or ""
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    local = socket.gethostname().lower()
    return name.lower() in ("localhost", local, local + ".local", local.split(".")[0])


def create_app(controller: Any, *, trace: Any = None, setup_dir: Path | None = None):
    """Build the Flask application."""
    from flask import Flask, abort, jsonify, render_template, request, send_file, send_from_directory, session

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=_session_signing_key(setup_dir),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        PERMANENT_SESSION_LIFETIME=timedelta(days=REMEMBER_DAYS),
        MAX_CONTENT_LENGTH=MAX_BODY_BYTES,
        JSON_SORT_KEYS=False,
    )
    throttle = LoginThrottle()
    password_lock = threading.Lock()
    hostname = socket.gethostname()

    def cfg():
        return controller.cfg

    def setup_available() -> bool:
        if setup_dir is None:
            return False
        pending, claimed = setup_dir / "web-setup-pending", setup_dir / "web-setup-claimed"
        auth = Path(cfg().web.auth_file)
        if auth.exists() or auth.is_symlink():
            if pending.exists():
                try:
                    close_initial_setup(setup_dir)
                except OSError:
                    log.exception("cannot close initial web setup")
            return False
        return pending.is_file() and not claimed.exists()

    with password_lock:
        setup_available()

    def current_user() -> tuple[str, str] | None:
        user = session.get("user")
        if not user:
            return None
        account = load_accounts(cfg().web.auth_file).get(user)
        fingerprint = hashlib.sha256(account[1].encode()).hexdigest() if account else ""
        if (account is None or account[0] != session.get("role")
                or not hmac.compare_digest(fingerprint, session.get("credential", ""))):
            session.clear()
            return None
        return user, account[0]

    def error(status: int, message: str):
        return jsonify({"ok": False, "error": message}), status

    @app.before_request
    def checks():
        if not _allowed_host(request.host):
            abort(400)
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and urlparse(origin).netloc != request.host:
                return error(403, "cross-origin request rejected")
            if request.endpoint != "login":
                token = session.get("csrf")
                sent = request.headers.get("X-CSRF-Token", "")
                if not token or not hmac.compare_digest(token, sent):
                    return error(403, "missing or invalid CSRF token")
        if request.endpoint not in ("index", "login", "static", "session_info", "initial_setup") and current_user() is None:
            return error(401, "login required")
        if session.get("change_password_required") and request.endpoint not in (
            "index", "static", "session_info", "change_password", "logout"
        ):
            return error(403, "change the default administrator password first")
        return None

    @app.after_request
    def headers(resp):
        tile_origin = "{uri.scheme}://{uri.netloc}".format(uri=urlparse(cfg().web.tile_url.replace("{s}", "a")))
        tile_src = tile_origin.replace("://a.", "://*.") + " " + tile_origin if "{s}" in cfg().web.tile_url else tile_origin
        resp.headers["Content-Security-Policy"] = (
            f"default-src 'self'; img-src 'self' data: {tile_src}; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        resp.headers["Cache-Control"] = resp.headers.get("Cache-Control", "no-store")
        return resp

    def run(action: str, **kwargs: Any):
        user = current_user()
        try:
            result = controller.call(action, requester=f"web:{user[0] if user else '?'}", **kwargs)
        except CommandError as exc:
            msg = str(exc)
            return error(409 if msg.startswith("conflict") else 400, msg)
        return jsonify({"ok": True, "result": result})

    # -- pages and session ---------------------------------------------------

    @app.get("/")
    def index():
        return render_template("index.html", version=__version__, hostname=hostname)

    @app.get("/api/session")
    def session_info():
        user = current_user()
        if user is None:
            with password_lock:
                available = setup_available()
            if available:
                session.setdefault("csrf", secrets.token_urlsafe(32))
                return jsonify({"user": None, "setup_required": True, "csrf": session["csrf"]})
            return jsonify({"user": None, "setup_required": False})
        return jsonify({"user": user[0], "role": user[1], "csrf": session.get("csrf"), "hostname": hostname,
                        "change_password_required": bool(session.get("change_password_required"))})

    @app.post("/api/login")
    def login():
        addr = request.remote_addr or "?"
        if throttle.blocked(addr):
            return error(429, "too many failed logins; try again later")
        data = request.get_json(silent=True) or {}
        password = data.get("password")
        from werkzeug.security import check_password_hash

        matches = []
        if isinstance(password, str) and 1 <= len(password) <= 1024:
            for username, account in load_accounts(cfg().web.auth_file).items():
                if check_password_hash(account[1], password):
                    matches.append((username, account))
        if len(matches) != 1:
            throttle.failed(addr)
            log.warning("web login failed from %s", addr)
            return error(401, "invalid password")
        user, account = matches[0]
        throttle.succeeded(addr)
        session.clear()
        remember = data.get("remember") is True
        session.permanent = remember
        change_required = user == "admin" and account[0] == "admin" and check_password_hash(account[1], "geofence")
        session.update(user=user, role=account[0], csrf=secrets.token_urlsafe(32),
                       credential=hashlib.sha256(account[1].encode()).hexdigest(),
                       change_password_required=change_required)
        log.info("web login: %s (%s) from %s", user, account[0], addr)
        return jsonify({"ok": True, "user": user, "role": account[0], "csrf": session["csrf"],
                        "hostname": hostname, "change_password_required": change_required})

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify({"ok": True})

    @app.post("/api/setup")
    def initial_setup():
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return error(400, "JSON object required")
        password = data.get("password")
        if not isinstance(password, str) or not 8 <= len(password) <= 1024 or password == "geofence":
            return error(400, "password must be 8 to 1024 characters and not the old default")
        from werkzeug.security import generate_password_hash

        with password_lock:
            if setup_dir is None or not setup_available():
                return error(409, "initial setup is not available")
            try:
                path = managed_secret_path(controller.config_path, cfg().web.auth_file)
                others = (cfg().openhop.api_key_file, cfg().meshmapper.api_key_file, cfg().companion.ble_pin_file)
                if any(path.resolve() == Path(other).resolve() for other in others if other):
                    raise ValueError("account file path is also used by another credential")
                account = f"admin:admin:{generate_password_hash(password)}\n".encode()
                if not close_initial_setup(setup_dir):
                    return error(409, "initial setup has already been claimed")
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
                with os.fdopen(fd, "wb") as fh:
                    os.fchmod(fh.fileno(), 0o640)
                    fh.write(account)
                    fh.flush()
                    os.fsync(fh.fileno())
                dir_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except (OSError, ValueError) as exc:
                log.error("initial web setup failed: %s", exc)
                return error(500, "setup failed; create an account with sudo openhop-geofence passwd admin")
        session.clear()
        return jsonify({"ok": True, "result": "administrator created; log in"})

    @app.post("/api/password")
    def change_password():
        user = current_user()
        if user is None or user[1] != "admin":
            return error(403, "administrator required")
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return error(400, "JSON object required")
        old, new = data.get("current_password"), data.get("new_password")
        if not isinstance(old, str) or not isinstance(new, str) or len(new) < 8 or len(new) > 1024:
            return error(400, "new password must be between 8 and 1024 characters")
        if new == "geofence":
            return error(400, "choose a password other than the default")
        from werkzeug.security import check_password_hash

        with password_lock:
            account = load_accounts(cfg().web.auth_file).get(user[0])
            if account is None or account[0] != user[1] or not check_password_hash(account[1], old):
                return error(403, "current password is incorrect")
            try:
                path = managed_secret_path(controller.config_path, cfg().web.auth_file)
                others = (cfg().openhop.api_key_file, cfg().meshmapper.api_key_file, cfg().companion.ble_pin_file)
                if any(path.resolve() == Path(other).resolve() for other in others if other):
                    raise ValueError("account file path is also used by another credential")
                set_password(str(path), user[0], user[1], new)
            except (OSError, ValueError) as exc:
                return error(500, f"cannot change password: {exc}")
        session.clear()
        return jsonify({"ok": True, "result": "password changed; log in again"})

    @app.post("/api/secrets")
    def update_secret():
        user = current_user()
        if user is None or user[1] != "admin":
            return error(403, "administrator required")
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return error(400, "JSON object required")
        kind, value = data.get("kind"), data.get("value")
        if not isinstance(kind, str) or not isinstance(value, str):
            return error(400, "secret kind and value are required")
        return run("update_secret", kind=kind, value=value)

    # -- status and control ----------------------------------------------------

    @app.get("/api/status")
    def status():
        snap = controller.status()
        snap.setdefault("companion", {"enabled": False})
        snap["trace"] = trace.status() if trace else {"enabled": False}
        snap["web"] = {"map_enabled": cfg().web.map_enabled, "config_edit": cfg().web.allow_config_edit}
        return jsonify(snap)

    @app.post("/api/action")
    def action():
        name = (request.get_json(silent=True) or {}).get("action")
        if name not in ACTIONS:
            return error(400, "unknown action")
        act, kwargs = ACTIONS[name]
        return run(act, **kwargs)

    @app.post("/api/maintenance")
    def maintenance():
        user = current_user()
        if user is None or user[1] != "admin":
            return error(403, "administrator required")
        data = request.get_json(silent=True) or {}
        name = data.get("action")
        if name == "restart":
            return run("restart")
        if name == "reboot":
            return run("reboot")
        return error(400, "unknown maintenance action")

    # -- configuration and zones ---------------------------------------------------

    @app.get("/api/config")
    def get_config():
        try:
            text = controller.config_path.read_text(encoding="utf-8")
        except OSError as exc:
            return error(500, f"cannot read configuration: {exc}")
        return jsonify({
            "text": text, "revision": config_mod.config_revision(text),
            "editable": cfg().web.allow_config_edit,
        })

    @app.post("/api/config")
    def save_config():
        if not cfg().web.allow_config_edit:
            return error(403, "configuration editing is disabled")
        data = request.get_json(silent=True) or {}
        text, revision = data.get("text"), data.get("revision")
        if not isinstance(text, str) or not isinstance(revision, str):
            return error(400, "text and revision are required")
        return run("save_config", text=text, revision=revision, role=current_user()[1])

    @app.get("/api/zones")
    def get_zones():
        try:
            text = controller.config_path.read_text(encoding="utf-8")
        except OSError as exc:
            return error(500, f"cannot read configuration: {exc}")
        import tomllib

        try:
            raw = tomllib.loads(text).get("zones", [])
        except tomllib.TOMLDecodeError as exc:
            return error(500, f"configuration is not valid TOML: {exc}")
        geojson = {"type": "FeatureCollection", "features": [z.to_geojson() for z in cfg().zones]}
        return jsonify({"zones": raw, "geojson": geojson, "revision": config_mod.config_revision(text)})

    @app.post("/api/zones")
    def save_zones():
        if not cfg().web.allow_config_edit:
            return error(403, "configuration editing is disabled")
        data = request.get_json(silent=True) or {}
        revision = data.get("revision")
        try:
            text = controller.config_path.read_text(encoding="utf-8")
        except OSError as exc:
            return error(500, f"cannot read configuration: {exc}")
        if revision != config_mod.config_revision(text):
            return error(409, "conflict: configuration changed since it was loaded")
        try:
            new_text = replace_zones(text, data.get("zones"))
        except ValueError as exc:
            return error(400, str(exc))
        return run("save_config", text=new_text, revision=revision, role=current_user()[1])

    # -- map ---------------------------------------------------------------------------

    @app.get("/api/map")
    def map_info():
        w = cfg().web
        leaflet = Path(w.leaflet_dir)
        return jsonify({
            "enabled": w.map_enabled,
            "leaflet": w.map_enabled and (leaflet / "leaflet.js").is_file(),
            "leaflet_dir": w.leaflet_dir,
            "tile_url": w.tile_url,
            "attribution": w.tile_attribution,
        })

    @app.get("/api/coverage")
    def coverage():
        """Cached MeshMapper squares near a point (no network I/O)."""
        mm = getattr(controller, "meshmapper", None)
        try:
            lat, lon = float(request.args["lat"]), float(request.args["lon"])
        except (KeyError, ValueError):
            return error(400, "lat and lon are required")
        if mm is None:
            return jsonify({"squares": []})
        squares = mm.squares_near(lat, lon, radius_deg=0.05)[:5000]
        return jsonify({"squares": [
            {"grid_id": s.grid_id, "bounds": [s.south, s.west, s.north, s.east], "type": s.type,
             "class": mm.classify(s)}
            for s in squares
        ]})

    @app.get("/leaflet/<path:name>")
    def leaflet(name: str):
        w = cfg().web
        if not w.map_enabled:
            abort(404)
        resp = send_from_directory(w.leaflet_dir, name, max_age=86400)
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp

    # -- diagnostics trace ---------------------------------------------------------------

    @app.get("/api/trace")
    def trace_data():
        if trace is None:
            return error(404, "diagnostics are disabled")
        records, context = trace.snapshot()
        return jsonify({"records": records[-20000:], "context": context})

    @app.post("/api/trace")
    def trace_action():
        if trace is None:
            return error(404, "diagnostics are disabled")
        name = (request.get_json(silent=True) or {}).get("action")
        try:
            if name == "start":
                trace.set_recording(True)
            elif name == "stop":
                trace.set_recording(False)
            elif name == "clear":
                trace.clear()
            else:
                return error(400, "unknown trace action")
        except RuntimeError as exc:
            return error(400, str(exc))
        log.info("trace %s by web:%s", name, current_user()[0])
        return jsonify({"ok": True, "result": f"trace {name}"})

    @app.get("/api/trace/export")
    def trace_export():
        if trace is None:
            return error(404, "diagnostics are disabled")
        fh = trace.export_zip()
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        return send_file(fh, mimetype="application/zip", as_attachment=True,
                         download_name=f"openhop-geofence-trace-{stamp}.zip")

    return app


class WebServer:
    """Runs the Flask app under Waitress in a background thread."""

    def __init__(self, app: Any, bind: str, port: int):
        self.app, self.bind, self.port = app, bind, port
        self.server: Any = None
        self.error: str | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        from waitress import create_server

        try:
            self.server = create_server(self.app, host=self.bind, port=self.port, threads=4,
                                        connection_limit=32, ident="openhop-geofence",
                                        max_request_body_size=MAX_BODY_BYTES)
        except OSError as exc:
            # Mode control continues without the web interface.
            self.error = f"cannot bind {self.bind}:{self.port}: {exc}"
            log.error("web interface unavailable: %s", self.error)
            return False
        self._thread = threading.Thread(target=self._serve, name="web", daemon=True)
        self._thread.start()
        log.info("web interface listening on http://%s:%d/", self.bind, self.port)
        return True

    def _serve(self) -> None:
        try:
            self.server.run()
        except Exception as exc:  # Includes the normal close during shutdown.
            if self.server is not None:
                log.debug("web server stopped: %s", exc)

    def stop(self) -> None:
        if self.server is not None:
            server, self.server = self.server, None
            server.close()


def ensure_auth_file_warning(path: str) -> str | None:
    """Return a warning if no account can log in."""
    if not os.path.exists(path):
        return f"web auth file {path} does not exist; complete initial web setup if available or use 'openhop-geofence passwd'"
    if not load_accounts(path):
        return f"web auth file {path} contains no valid accounts"
    return None
