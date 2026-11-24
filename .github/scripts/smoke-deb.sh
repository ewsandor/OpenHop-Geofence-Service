#!/bin/sh
# Install the built .deb in a clean Debian container and check that it works.
# Usage: smoke-deb.sh path/to/openhop-geofence_X_all.deb
set -eu
export DEBIAN_FRONTEND=noninteractive
deb=$(readlink -f "$1")
apt-get update -qq
# dpkg -i alone cannot resolve dependencies; apt-get -f install completes them.
dpkg -i "$deb" || apt-get install -y -qq -f
apt-get install -y -qq python3-shapely python3-flask python3-waitress python3-tomlkit \
    libjs-leaflet systemd >/dev/null
dpkg -s openhop-geofence | grep -E '^(Status|Version):'
openhop-geofence --version
openhop-geofence-trace --help >/dev/null
python3 -c 'import shapely, flask, waitress, tomlkit, openhop_geofence.web, openhop_geofence.meshmapper; print("imports ok")'
getent passwd openhop-geofence
test "$(stat -c %a /etc/openhop-geofence)" = 1770
test "$(stat -c %a /etc/openhop-geofence/secrets)" = 750
test "$(stat -c %U /etc/openhop-geofence/config.toml)" = openhop-geofence
su -s /bin/sh -c 'openhop-geofence --check-config' openhop-geofence
# /bin/kill is reported missing in minimal containers; only fail on other errors.
out=$(systemd-analyze verify /usr/lib/systemd/system/openhop-geofence.service 2>&1 || true)
echo "$out" | grep -v -e 'kill is not executable' -e '^$' && exit 1
test "$(systemctl is-enabled openhop-geofence || true)" = disabled
apt-get purge -y -qq openhop-geofence >/dev/null
test ! -e /etc/openhop-geofence/config.toml
echo "smoke test passed"
