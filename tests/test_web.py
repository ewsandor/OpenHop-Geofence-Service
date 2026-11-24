"""Web interface authentication, CSRF, roles, and editing (design test 12)."""

import tempfile
import unittest
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
        auth = d / "users"
        web.set_password(str(auth), "admin", "admin", "admin-password")
        web.set_password(str(auth), "op", "operator", "operator-password")
        cfg = d / "config.toml"
        cfg.write_text(CONFIG.format(auth=auth))
        self.ctl = StubController(cfg)
        self.app = web.create_app(self.ctl)
        self.hostname = __import__("socket").gethostname()

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

    def test_maintenance_roles_and_reboot_confirmation(self):
        c, csrf = self.login("op", "operator-password")
        self.assertEqual(c.post("/api/maintenance", json={"action": "restart"}, headers=csrf).status_code, 403)
        c, csrf = self.login()
        r = c.post("/api/maintenance", json={"action": "reboot", "confirm_host": "wrong"}, headers=csrf)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.ctl.calls, [])
        r = c.post("/api/maintenance", json={"action": "reboot", "confirm_host": self.hostname}, headers=csrf)
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


if __name__ == "__main__":
    unittest.main()
