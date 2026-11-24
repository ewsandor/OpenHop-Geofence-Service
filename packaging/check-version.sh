#!/bin/sh
# Check that the version is consistent across the project.
#
# Usage: packaging/check-version.sh [TAG]
#   TAG (optional), e.g. "v1.2.3", must match the version as well.
# Run from the repository root. Exits non-zero on any mismatch.
set -eu

py=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' src/openhop_geofence/__init__.py)
deb=$(sed -n '1s/^[^(]*(\([^)]*\)).*/\1/p' debian/changelog)
rpm=$(sed -n 's/^Version:[[:space:]]*//p' packaging/fedora/openhop-geofence.spec)
# Latest %changelog entry, e.g. "* Tue Nov 24 2026 Name <email> - 1.2.3-1"
rpmlog=$(sed -n '/^%changelog/,$ s/^\* .* - \([^-]*\)-[^-]*$/\1/p' packaging/fedora/openhop-geofence.spec | head -n 1)

echo "__init__.py:       $py"
echo "debian/changelog:  $deb"
echo "spec Version:      $rpm"
echo "spec %changelog:   $rpmlog"

status=0
for v in "$deb" "$rpm" "$rpmlog"; do
    [ "$v" = "$py" ] || status=1
done
if [ $# -gt 0 ]; then
    echo "tag:               $1"
    [ "$1" = "v$py" ] || status=1
fi
if [ $status -ne 0 ]; then
    echo "error: version mismatch" >&2
fi
exit $status
