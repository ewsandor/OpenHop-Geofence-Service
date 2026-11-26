#!/bin/sh
# Install the built .deb in a clean Debian container and check that it works.
# Usage: smoke-deb.sh path/to/openhop-geofence_X_all.deb
set -eu
export DEBIAN_FRONTEND=noninteractive
deb=$(readlink -f "$1")
apt-get update -qq
# Install without recommendations so missing required dependencies are exposed.
apt-get install -y -qq --no-install-recommends "$deb" > /tmp/openhop-geofence-smoke-install.log
cat /tmp/openhop-geofence-smoke-install.log
grep -q 'Set the admin password and continue setup on a trusted LAN:' /tmp/openhop-geofence-smoke-install.log
grep -Eq 'http://[^[:space:]]+:8080/' /tmp/openhop-geofence-smoke-install.log
apt-get install -y -qq --no-install-recommends systemd >/dev/null
dpkg -s openhop-geofence | grep -E '^(Status|Version):'
openhop-geofence --version
openhop-geofence-trace --help >/dev/null
python3 -c 'import shapely, flask, waitress, tomlkit, openhop_geofence.web, openhop_geofence.meshmapper; print("imports ok")'
test -r /usr/share/javascript/leaflet/leaflet.js
test -r /usr/share/polkit-1/rules.d/50-openhop-geofence-reboot.rules
dpkg-query -W -f='${Depends}\n' openhop-geofence | grep -qw polkitd
getent passwd openhop-geofence
test "$(stat -c %a /etc/openhop-geofence)" = 2775
test "$(stat -c %a /etc/openhop-geofence/secrets)" = 2755
test "$(stat -c %U:%G /etc/openhop-geofence/secrets)" = openhop-geofence:openhop-geofence
test "$(stat -c %U /etc/openhop-geofence/config.toml)" = openhop-geofence
test "$(stat -c %G /etc/openhop-geofence/config.toml)" = openhop-geofence
test "$(stat -c %a /etc/openhop-geofence/config.toml)" = 664
test ! -e /etc/openhop-geofence/secrets/web-auth
test "$(stat -c %U:%G:%a /var/lib/openhop-geofence/web-setup-pending)" = openhop-geofence:openhop-geofence:600
su -s /bin/sh -c "python3 -c 'from openhop_geofence.state import SafeModeStore, TxLockoutStore; p=\"/var/lib/openhop-geofence\"; assert not SafeModeStore(p).load().active and not TxLockoutStore(p).load().active'" openhop-geofence
su -s /bin/sh -c "python3 -c 'from pathlib import Path; from types import SimpleNamespace; from openhop_geofence.config import load_config; from openhop_geofence.web import create_app; p=Path(\"/etc/openhop-geofence/config.toml\"); c=SimpleNamespace(config_path=p, cfg=load_config(p)); client=create_app(c, setup_dir=Path(\"/var/lib/openhop-geofence\")).test_client(); s=client.get(\"/api/session\").get_json(); assert s[\"setup_required\"]; r=client.post(\"/api/setup\", json={\"password\": \"changed-password\"}, headers={\"X-CSRF-Token\": s[\"csrf\"]}); assert r.status_code == 200, r.get_json()'" openhop-geofence
test ! -e /var/lib/openhop-geofence/web-setup-pending
test -e /var/lib/openhop-geofence/web-setup-claimed
test "$(stat -c %U:%G:%a /var/lib/openhop-geofence/web-session.key)" = openhop-geofence:openhop-geofence:600
test "$(stat -c %U:%G:%a /etc/openhop-geofence/secrets/web-auth)" = openhop-geofence:openhop-geofence:640
su -s /bin/sh -c 'set -e; ls /etc/openhop-geofence /etc/openhop-geofence/secrets >/dev/null; cat /etc/openhop-geofence/config.toml >/dev/null; if test -w /etc/openhop-geofence/config.toml || cat /etc/openhop-geofence/secrets/web-auth >/dev/null 2>&1; then exit 1; fi' nobody
useradd -M -g openhop-geofence geofence-permissions-test
su -s /bin/sh -c "printf '\n# group edit\n' >> /etc/openhop-geofence/config.toml" geofence-permissions-test
python3 -c 'import os; p="/etc/openhop-geofence/config.toml"; fd=os.open(p, os.O_WRONLY | os.O_CREAT, 0o666); os.close(fd)'
su -s /bin/sh -c "python3 -c 'from pathlib import Path; from openhop_geofence.state import atomic_write; p = Path(\"/etc/openhop-geofence/config.toml\"); atomic_write(p, p.read_bytes())'" openhop-geofence
test "$(stat -c %U:%G:%a /etc/openhop-geofence/config.toml)" = openhop-geofence:openhop-geofence:664
su -s /bin/sh -c "python3 -c 'from openhop_geofence.web import load_accounts; from werkzeug.security import check_password_hash; role, password = load_accounts(\"/etc/openhop-geofence/secrets/web-auth\")[\"admin\"]; assert role == \"admin\" and check_password_hash(password, \"changed-password\")'" openhop-geofence
cp /etc/openhop-geofence/secrets/web-auth /tmp/openhop-geofence-smoke-auth
dpkg-query -W -f='${Conffiles}\n' openhop-geofence | grep -q '^ /etc/openhop-geofence/config.toml '
su -s /bin/sh -c 'openhop-geofence --check-config' openhop-geofence
su -s /bin/sh -c "python3 -c 'from openhop_geofence.config import load_config; assert load_config(\"/etc/openhop-geofence/config.toml\").maintenance.allow_reboot'" openhop-geofence
su -s /bin/sh -c "python3 -c 'from openhop_geofence.config import load_config; assert not load_config(\"/etc/openhop-geofence/config.toml\").meshmapper.enabled'" openhop-geofence
su -s /bin/sh -c "python3 -c 'from openhop_geofence.state import SafeModeLatch, SafeModeStore; SafeModeStore(\"/var/lib/openhop-geofence\").save(SafeModeLatch())'" openhop-geofence
# Simulate an upgrade where both the local config and packaged default changed.
su -s /bin/sh -c "printf '\n# smoke test edit\n' >> /etc/openhop-geofence/config.toml" openhop-geofence
chmod 0600 /etc/openhop-geofence/config.toml
dpkg-deb -R "$deb" /tmp/openhop-geofence-smoke-package
printf '\n# new packaged default\n' >> /tmp/openhop-geofence-smoke-package/etc/openhop-geofence/config.toml
dpkg-deb -b /tmp/openhop-geofence-smoke-package /tmp/openhop-geofence-smoke-upgrade.deb
su -s /bin/sh -c "python3 -c 'from openhop_geofence.state import SafeModeLatch, SafeModeStore; SafeModeStore(\"/var/lib/openhop-geofence\").save(SafeModeLatch(True, \"initial commissioning\", None))'" openhop-geofence
dpkg --force-confold -i /tmp/openhop-geofence-smoke-upgrade.deb
su -s /bin/sh -c "python3 -c 'from openhop_geofence.state import SafeModeStore; assert not SafeModeStore(\"/var/lib/openhop-geofence\").load().active'" openhop-geofence
su -s /bin/sh -c "python3 -c 'from openhop_geofence.state import SafeModeLatch, SafeModeStore; SafeModeStore(\"/var/lib/openhop-geofence\").save(SafeModeLatch(True, \"Disable all TX requested\", None))'" openhop-geofence
dpkg --force-confold -i "$deb"
su -s /bin/sh -c "python3 -c 'from openhop_geofence.state import SafeModeStore, TxLockoutStore; p=\"/var/lib/openhop-geofence\"; assert not SafeModeStore(p).load().active and TxLockoutStore(p).load().active'" openhop-geofence
su -s /bin/sh -c "python3 -c 'from openhop_geofence.state import TxLockout, TxLockoutStore; TxLockoutStore(\"/var/lib/openhop-geofence\").save(TxLockout())'" openhop-geofence
test "$(stat -c %a /etc/openhop-geofence/config.toml)" = 664
cmp /tmp/openhop-geofence-smoke-auth /etc/openhop-geofence/secrets/web-auth
grep -q '^# smoke test edit$' /etc/openhop-geofence/config.toml
grep -q '^# new packaged default$' /etc/openhop-geofence/config.toml.dpkg-dist
dpkg --force-confnew -i "$deb"
cmp /usr/share/openhop-geofence/config.toml.example /etc/openhop-geofence/config.toml
test "$(stat -c %U /etc/openhop-geofence/config.toml)" = openhop-geofence
# A deliberately deleted conffile remains absent unless explicitly restored.
rm /etc/openhop-geofence/config.toml
dpkg -i "$deb"
test ! -e /etc/openhop-geofence/config.toml
dpkg --force-confmiss -i "$deb"
cmp /usr/share/openhop-geofence/config.toml.example /etc/openhop-geofence/config.toml
# An auth file deliberately removed after installation is not recreated on upgrade.
rm /etc/openhop-geofence/secrets/web-auth
dpkg -i "$deb"
test ! -e /etc/openhop-geofence/secrets/web-auth
test ! -e /var/lib/openhop-geofence/web-setup-pending
install -m 0640 -o root -g openhop-geofence /tmp/openhop-geofence-smoke-auth /etc/openhop-geofence/secrets/web-auth
# /bin/kill is reported missing in minimal containers; only fail on other errors.
out=$(systemd-analyze verify /usr/lib/systemd/system/openhop-geofence.service 2>&1 || true)
echo "$out" | grep -v -e 'kill is not executable' -e '^$' && exit 1
test "$(systemctl is-enabled openhop-geofence || true)" = enabled
apt-get purge -y -qq openhop-geofence >/dev/null
test ! -e /etc/openhop-geofence/config.toml
test ! -e /usr/share/polkit-1/rules.d/50-openhop-geofence-reboot.rules
# Older packages created the config themselves instead of registering it.
mkdir -p /etc/openhop-geofence
cp /tmp/openhop-geofence-smoke-package/usr/share/openhop-geofence/config.toml.example \
    /etc/openhop-geofence/config.toml
printf '\n# legacy local edit\n' >> /etc/openhop-geofence/config.toml
dpkg --force-confold -i "$deb"
cmp /tmp/openhop-geofence-smoke-auth /etc/openhop-geofence/secrets/web-auth
grep -q '^# legacy local edit$' /etc/openhop-geofence/config.toml
test "$(stat -c %U /etc/openhop-geofence/config.toml)" = openhop-geofence
su -s /bin/sh -c 'test -w /etc/openhop-geofence/config.toml' openhop-geofence
dpkg-query -W -f='${Conffiles}\n' openhop-geofence | grep -q '^ /etc/openhop-geofence/config.toml '
apt-get purge -y -qq openhop-geofence >/dev/null
echo "smoke test passed"
