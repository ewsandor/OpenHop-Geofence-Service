#!/bin/sh
# Install the openhop-geofence systemd service for a pip or source install.
#
# Debian and Fedora packages do this automatically; use this script only for
# portable installs. It is idempotent: it creates the service account and
# directories, installs the unit and reboot authorization rule, and copies
# the example configuration only if none exists. It never enables or starts
# the service, never overwrites an
# existing configuration, and never changes boot or logging settings.
#
# Usage (from an extracted source tarball or a git checkout):
#   sudo packaging/install-systemd.sh [--python /opt/openhop-geofence/bin/python]
#
# --python  Interpreter that has openhop_geofence installed, typically a
#           root-owned virtual environment. Without it, the unit runs the
#           "openhop-geofence" command found on PATH.

set -eu

NAME=openhop-geofence
CONF_DIR=/etc/$NAME
UNIT_DIR=/etc/systemd/system
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=$(dirname "$HERE")
PYTHON=

while [ $# -gt 0 ]; do
    case "$1" in
        --python) PYTHON=${2:?--python requires a path}; shift 2 ;;
        -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root (sudo)" >&2
    exit 1
fi

# Resolve the command the unit will run.
if [ -n "$PYTHON" ]; then
    [ -x "$PYTHON" ] || { echo "not executable: $PYTHON" >&2; exit 1; }
    "$PYTHON" -c "import openhop_geofence" || { echo "openhop_geofence is not installed for $PYTHON" >&2; exit 1; }
    EXEC="$PYTHON -m openhop_geofence"
else
    EXEC=$(command -v "$NAME" || true)
    [ -n "$EXEC" ] || { echo "$NAME not found on PATH; use --python" >&2; exit 1; }
fi

# Unprivileged system account (no login shell, no home directory).
if ! getent passwd "$NAME" >/dev/null; then
    useradd --system --user-group --no-create-home --home-dir "/var/lib/$NAME" \
        --shell /usr/sbin/nologin --comment "OpenHop geofence service" "$NAME"
    echo "created system account $NAME"
fi

# The service and its group can replace the configuration. Secrets are
# managed by the service but remain unreadable to other local users.
install -d -m 2775 -o root -g "$NAME" "$CONF_DIR"
install -d -m 2755 -o "$NAME" -g "$NAME" "$CONF_DIR/secrets"
if [ ! -e "$CONF_DIR/config.toml" ]; then
    install -m 0664 -o "$NAME" -g "$NAME" "$SRC/config/config.toml.example" "$CONF_DIR/config.toml"
    echo "installed example configuration $CONF_DIR/config.toml"
fi

# Unit file with the resolved command.
sed "s#^ExecStart=/usr/bin/$NAME #ExecStart=$EXEC #" "$HERE/$NAME.service" > "$UNIT_DIR/$NAME.service.tmp"
chmod 0644 "$UNIT_DIR/$NAME.service.tmp"
mv "$UNIT_DIR/$NAME.service.tmp" "$UNIT_DIR/$NAME.service"
install -D -m 0644 "$HERE/50-$NAME-reboot.rules" \
    "/usr/share/polkit-1/rules.d/50-$NAME-reboot.rules"
systemctl daemon-reload

cat <<EOF
Installed $UNIT_DIR/$NAME.service (not enabled).
Next steps:
  1. Edit $CONF_DIR/config.toml and add secrets under $CONF_DIR/secrets/
     (root or $NAME:$NAME, mode 0640).
  2. Check it:   sudo -u $NAME $EXEC --check-config --config $CONF_DIR/config.toml
  3. Web login:  sudo $EXEC passwd admin --auth-file $CONF_DIR/secrets/web-auth
  4. Start:      sudo systemctl enable --now $NAME
Host reboot requires systemd-logind and polkit; set maintenance.allow_reboot
to false in the local configuration to disable reboot requests.
EOF
