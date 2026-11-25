#!/bin/sh
# Install the built .deb in a clean Debian container and check that it works.
# Usage: smoke-deb.sh path/to/openhop-geofence_X_all.deb
set -eu
export DEBIAN_FRONTEND=noninteractive
deb=$(readlink -f "$1")
apt-get update -qq
# Install without recommendations so missing required dependencies are exposed.
apt-get install -y -qq --no-install-recommends "$deb"
apt-get install -y -qq --no-install-recommends systemd >/dev/null
dpkg -s openhop-geofence | grep -E '^(Status|Version):'
openhop-geofence --version
openhop-geofence-trace --help >/dev/null
python3 -c 'import shapely, flask, waitress, tomlkit, openhop_geofence.web, openhop_geofence.meshmapper; print("imports ok")'
test -r /usr/share/javascript/leaflet/leaflet.js
getent passwd openhop-geofence
test "$(stat -c %a /etc/openhop-geofence)" = 1770
test "$(stat -c %a /etc/openhop-geofence/secrets)" = 750
test "$(stat -c %U /etc/openhop-geofence/config.toml)" = openhop-geofence
test "$(stat -c %a /etc/openhop-geofence/config.toml)" = 640
dpkg-query -W -f='${Conffiles}\n' openhop-geofence | grep -q '^ /etc/openhop-geofence/config.toml '
su -s /bin/sh -c 'openhop-geofence --check-config' openhop-geofence
# Simulate an upgrade where both the local config and packaged default changed.
su -s /bin/sh -c "printf '\n# smoke test edit\n' >> /etc/openhop-geofence/config.toml" openhop-geofence
dpkg-deb -R "$deb" /tmp/openhop-geofence-smoke-package
printf '\n# new packaged default\n' >> /tmp/openhop-geofence-smoke-package/etc/openhop-geofence/config.toml
dpkg-deb -b /tmp/openhop-geofence-smoke-package /tmp/openhop-geofence-smoke-upgrade.deb
dpkg --force-confold -i /tmp/openhop-geofence-smoke-upgrade.deb
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
# /bin/kill is reported missing in minimal containers; only fail on other errors.
out=$(systemd-analyze verify /usr/lib/systemd/system/openhop-geofence.service 2>&1 || true)
echo "$out" | grep -v -e 'kill is not executable' -e '^$' && exit 1
test "$(systemctl is-enabled openhop-geofence || true)" = disabled
apt-get purge -y -qq openhop-geofence >/dev/null
test ! -e /etc/openhop-geofence/config.toml
# Older packages created the config themselves instead of registering it.
mkdir -p /etc/openhop-geofence
cp /tmp/openhop-geofence-smoke-package/usr/share/openhop-geofence/config.toml.example \
    /etc/openhop-geofence/config.toml
printf '\n# legacy local edit\n' >> /etc/openhop-geofence/config.toml
dpkg --force-confold -i "$deb"
grep -q '^# legacy local edit$' /etc/openhop-geofence/config.toml
test "$(stat -c %U /etc/openhop-geofence/config.toml)" = openhop-geofence
su -s /bin/sh -c 'test -w /etc/openhop-geofence/config.toml' openhop-geofence
dpkg-query -W -f='${Conffiles}\n' openhop-geofence | grep -q '^ /etc/openhop-geofence/config.toml '
apt-get purge -y -qq openhop-geofence >/dev/null
echo "smoke test passed"
