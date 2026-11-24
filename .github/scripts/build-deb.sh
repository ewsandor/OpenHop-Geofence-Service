#!/bin/sh
# Build the Debian package. Runs as root in a debian:trixie container with the
# repository mounted at the current directory; writes dist/*.deb.
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends build-essential debhelper dh-python \
    python3-all python3-setuptools pybuild-plugin-pyproject >/dev/null
src=$(pwd)
rm -rf /tmp/build && mkdir -p /tmp/build/openhop-geofence
cp -a . /tmp/build/openhop-geofence/
cd /tmp/build/openhop-geofence
dpkg-buildpackage -us -uc -b
mkdir -p "$src/dist"
cp ../*.deb "$src/dist/"
