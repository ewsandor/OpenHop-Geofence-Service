"""Web interface authentication, CSRF, roles, and editing (design test 12)."""

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fakes  # noqa: F401  (quiet logging)
from openhop_geofence import config
from openhop_geofence.controller import CommandError

try:
    import flask  # noqa: F401
    import tomlkit  # noqa: F401

    from openhop_geofence import web

    HAVE_WEB = True
except ImportError:
    HAVE_WEB = False

CONFIG = """\
[web]
enabled = true
allow_config_edit = true
auth_file = "{auth}"

[[zones]]
id = "home"
role = "monitor"
latitude = 40.0
longitude = -105.0
radius_m = 500
"""


class StubController:
    """Records commands; implements only what the web layer uses."""

    def __init__(self, path: Path):
        self.config_path = path
        self.cfg = config.load_config(path)
        self.calls = []
        self.meshmapper = None

    def call(self, action, **kw):
        self.calls.append((action, kw))
        if action == "save_config":
            if kw["revision"] != config.config_revision(self.config_path.read_text()):
                raise CommandError("conflict: configuration changed since it was loaded")
            config.parse_config(kw["text"])
            self.config_path.write_text(kw["text"])
        return "ok"

    def status(self):
        return {"control": {"state": "monitor"}}


@unittest.skipUnless(HAVE_WEB, "Flask/tomlkit not installed")
class WebTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        auth = d / "secrets" / "users"
        web.set_password(str(auth), "admin", "admin", "admin-password")
        web.set_password(str(auth), "op", "operator", "operator-password")
        cfg = d / "config.toml"
        cfg.write_text(CONFIG.format(auth=auth))
        self.ctl = StubController(cfg)
        self.app = web.create_app(self.ctl)

    def login(self, user="admin", password="admin-password"):
        client = self.app.test_client()
        r = client.post("/api/login", json={"username": user, "password": password})
        self.assertEqual(r.status_code, 200, r.get_json())
        return client, {"X-CSRF-Token": r.get_json()["csrf"]}

    def test_login_required_everywhere(self):
        c = self.app.test_client()
        self.assertEqual(c.get("/").status_code, 200)  # Login page only.
        for path in ("/api/status", "/api/config", "/api/zones", "/api/map"):
            self.assertEqual(c.get(path).status_code, 401, path)
        self.assertEqual(c.post("/api/login", json={"username": "admin", "password": "x"}).status_code, 401)

    def test_map_page_sends_only_origin_to_https_tile_provider(self):
        response = self.app.test_client().get("/")
        self.assertEqual(response.headers["Referrer-Policy"], "strict-origin-when-cross-origin")
        self.assertIn("https://tile.openstreetmap.org", response.headers["Content-Security-Policy"])

    def test_login_throttle(self):
        c = self.app.test_client()
        for _ in range(web.LOGIN_MAX_FAILURES):
            c.post("/api/login", json={"username": "admin", "password": "wrong"})
        r = c.post("/api/login", json={"username": "admin", "password": "admin-password"})
        self.assertEqual(r.status_code, 429)

    def test_csrf_origin_and_host(self):
        c, csrf = self.login()
        self.assertEqual(c.post("/api/action", json={"action": "resume"}).status_code, 403)
        r = c.post("/api/action", json={"action": "resume"}, headers={**csrf, "Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(c.get("/api/status", headers={"Host": "evil.example"}).status_code, 400)
        r = c.post("/api/action", json={"action": "force_repeat"}, headers=csrf)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.ctl.calls[-1], ("override", {"mode": "forward", "requester": "web:admin"}))

    def test_maintenance_roles_and_reboot(self):
        c, csrf = self.login("op", "operator-password")
        self.assertEqual(c.post("/api/maintenance", json={"action": "restart"}, headers=csrf).status_code, 403)
        self.assertEqual(c.post("/api/maintenance", json={"action": "reboot"}, headers=csrf).status_code, 403)
        c, csrf = self.login()
        self.assertEqual(c.post("/api/maintenance", json={"action": "reboot"}).status_code, 403)
        self.assertEqual(self.ctl.calls, [])
        r = c.post("/api/maintenance", json={"action": "reboot"}, headers=csrf)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.ctl.calls[-1][0], "reboot")

    def test_config_edit_revision_conflict(self):
        c, csrf = self.login()
        data = c.get("/api/config").get_json()
        text = data["text"].replace("radius_m = 500", "radius_m = 600")
        self.ctl.config_path.write_text(data["text"] + "\n# edited elsewhere\n")
        r = c.post("/api/config", json={"text": text, "revision": data["revision"]}, headers=csrf)
        self.assertEqual(r.status_code, 409)

    def test_zone_editor_preserves_comments(self):
        c, csrf = self.login()
        self.ctl.config_path.write_text("# keep me\n" + self.ctl.config_path.read_text())
        data = c.get("/api/zones").get_json()
        zones = data["zones"] + [{"id": "trail", "role": "repeat", "latitude": 40.1,
                                  "longitude": -105.1, "radius_m": 200}]
        r = c.post("/api/zones", json={"zones": zones, "revision": data["revision"]}, headers=csrf)
        self.assertEqual(r.status_code, 200, r.get_json())
        saved = self.ctl.config_path.read_text()
        self.assertIn("# keep me", saved)
        self.assertEqual([z.id for z in config.parse_config(saved).zones], ["home", "trail"])

    def test_session_invalidated_when_account_removed(self):
        c, _ = self.login("op", "operator-password")
        Path(self.ctl.cfg.web.auth_file).write_text("")
        self.assertEqual(c.get("/api/status").status_code, 401)

    def test_default_password_must_be_changed_and_invalidates_sessions(self):
        auth = self.ctl.cfg.web.auth_file
        web.set_password(auth, "admin", "admin", "geofence")
        c, csrf = self.login("admin", "geofence")
        other, _ = self.login("admin", "geofence")
        self.assertTrue(c.get("/api/session").get_json()["change_password_required"])
        self.assertEqual(c.get("/api/status").status_code, 403)
        self.assertEqual(c.post("/api/password", json={"current_password": "geofence", "new_password": "new-password"}).status_code, 403)
        self.assertEqual(c.post("/api/password", json={"current_password": "wrong", "new_password": "new-password"}, headers=csrf).status_code, 403)
        self.assertEqual(c.post("/api/password", json={"current_password": "geofence", "new_password": "geofence"}, headers=csrf).status_code, 400)
        r = c.post("/api/password", json={"current_password": "geofence", "new_password": "new-password"}, headers=csrf)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertNotIn("new-password", str(r.get_json()))
        self.assertEqual(other.get("/api/session").get_json()["user"], None)
        self.assertEqual(c.get("/api/status").status_code, 401)
        self.login("admin", "new-password")

    def test_secret_updates_are_admin_only_and_csrf_protected(self):
        c, csrf = self.login("op", "operator-password")
        body = {"kind": "openhop", "value": "test-key"}
        self.assertEqual(c.post("/api/secrets", json=body, headers=csrf).status_code, 403)
        self.assertEqual(c.post("/api/password", json={"current_password": "operator-password", "new_password": "new-password"}, headers=csrf).status_code, 403)
        c, csrf = self.login()
        self.assertEqual(c.post("/api/secrets", json=body).status_code, 403)
        r = c.post("/api/secrets", json=body, headers=csrf)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("test-key", str(r.get_json()))
        self.assertEqual(self.ctl.calls[-1], ("update_secret", {**body, "requester": "web:admin"}))

    def test_password_change_rejects_symlink_account_file(self):
        auth = Path(self.ctl.cfg.web.auth_file)
        outside = auth.parent.parent / "outside"
        auth.rename(outside)
        auth.symlink_to(outside)
        c, csrf = self.login()
        r = c.post("/api/password", json={"current_password": "admin-password", "new_password": "new-password"}, headers=csrf)
        self.assertEqual(r.status_code, 500)
        self.assertIn("non-symlink", r.get_json()["error"])
        self.login()

    def test_initial_setup_is_one_time_and_requires_csrf(self):
        auth = Path(self.ctl.cfg.web.auth_file)
        auth.unlink()
        state = self.ctl.config_path.parent / "state"
        state.mkdir()
        (state / "web-setup-pending").touch()
        app = web.create_app(self.ctl, setup_dir=state)
        first, second = app.test_client(), app.test_client()
        session = first.get("/api/session").get_json()
        other = second.get("/api/session").get_json()
        self.assertTrue(session["setup_required"])
        self.assertEqual(first.get("/api/status").status_code, 401)
        self.assertEqual(first.post("/api/setup", json={"password": "new-password"}).status_code, 403)
        headers = {"X-CSRF-Token": session["csrf"]}
        self.assertEqual(first.post("/api/setup", json={"password": "new-password"},
                                    headers={**headers, "Origin": "http://evil.example"}).status_code, 403)
        self.assertEqual(first.post("/api/setup", json={"password": "short"}, headers=headers).status_code, 400)
        r = first.post("/api/setup", json={"password": "new-password"}, headers=headers)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertNotIn("new-password", str(r.get_json()))
        self.assertEqual(auth.stat().st_mode & 0o777, 0o640)
        self.assertTrue((state / "web-setup-claimed").exists())
        self.assertEqual(second.post("/api/setup", json={"password": "another-password"},
                                     headers={"X-CSRF-Token": other["csrf"]}).status_code, 409)
        self.assertEqual(first.post("/api/login", json={"username": "admin", "password": "new-password"}).status_code, 200)
        auth.unlink()
        restarted = web.create_app(self.ctl, setup_dir=state).test_client()
        self.assertFalse(restarted.get("/api/session").get_json()["setup_required"])

    def test_concurrent_initial_setup_has_one_winner(self):
        Path(self.ctl.cfg.web.auth_file).unlink()
        state = self.ctl.config_path.parent / "state"
        state.mkdir()
        (state / "web-setup-pending").touch()
        apps = [web.create_app(self.ctl, setup_dir=state) for _ in range(2)]
        barrier = threading.Barrier(2)

        def claim(app):
            c = app.test_client()
            token = c.get("/api/session").get_json()["csrf"]
            barrier.wait(timeout=10)
            return c.post("/api/setup", json={"password": "new-password"}, headers={"X-CSRF-Token": token}).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, apps))
        self.assertEqual(sorted(results), [200, 409])

    def test_existing_account_closes_pending_setup(self):
        state = self.ctl.config_path.parent / "state"
        state.mkdir()
        (state / "web-setup-pending").touch()
        app = web.create_app(self.ctl, setup_dir=state)
        self.assertFalse(app.test_client().get("/api/session").get_json()["setup_required"])
        Path(self.ctl.cfg.web.auth_file).unlink()
        self.assertFalse(app.test_client().get("/api/session").get_json()["setup_required"])

    def test_interrupted_setup_does_not_reopen(self):
        auth = Path(self.ctl.cfg.web.auth_file)
        auth.unlink()
        auth.parent.rmdir()
        state = self.ctl.config_path.parent / "state"
        state.mkdir()
        (state / "web-setup-pending").touch()
        app = web.create_app(self.ctl, setup_dir=state)
        client = app.test_client()
        token = client.get("/api/session").get_json()["csrf"]
        response = client.post("/api/setup", json={"password": "new-password"}, headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 500)
        self.assertIn("passwd admin", response.get_json()["error"])
        self.assertTrue((state / "web-setup-claimed").exists())
        self.assertFalse(app.test_client().get("/api/session").get_json()["setup_required"])


if __name__ == "__main__":
    unittest.main()
