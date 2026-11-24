#!/bin/sh
# Build the RPM from the sdist in dist/. Runs as root in a fedora container
# with the repository mounted at the current directory; writes dist/*.rpm.
set -eu
dnf -y -q install rpm-build python3-devel python3-setuptools python3-pip python3-wheel util-linux pyproject-rpm-macros systemd-rpm-macros >/dev/null
src=$(pwd)
top=/tmp/rpmbuild
mkdir -p "$top/SOURCES"
cp dist/openhop_geofence-*.tar.gz packaging/fedora/openhop-geofence.sysusers "$top/SOURCES/"
rpmbuild -ba --define "_topdir $top" packaging/fedora/openhop-geofence.spec
cp "$top"/RPMS/noarch/*.rpm "$top"/SRPMS/*.rpm "$src/dist/"
