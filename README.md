# OpenHop Geofence Service

`openhop-geofence` switches an [OpenHop](https://docs.openhop.dev/) MeshCore
repeater between **Repeat** and **Monitor** based on its GPS position. It is
intended for mobile or relocatable repeaters, for example a vehicle repeater
that should repeat only where it adds coverage.

The service is a small Python program. gpsd provides the position, the
OpenHop HTTP API controls the mode, and optional MeshMapper coverage data
decides between configured zones. It targets a Raspberry Pi Zero 2W running
Raspberry Pi OS Lite and runs on other Debian-based and mainstream Linux
distributions.

[DESIGN.md](DESIGN.md) is the detailed design and safety reference.

> **Radio regulations.** The operator is responsible for regional radio
> regulations, including airtime and duty-cycle limits for repeating,
> companion reports, and last-gasp messages. **This service does not measure
> or enforce airtime or duty cycle.** Confirm OpenHop's airtime and
> duty-cycle configuration before operation; refer to OpenHop's
> documentation and local regulations for specific settings.

> **Not a hardware interlock.** A crash, power loss, or unreachable OpenHop
> API can prevent a mode change. See DESIGN.md section 5.

## Features

- Circle and polygon **Repeat** and **Monitor zones**. Monitor takes
  priority where zones overlap.
- Configurable behavior outside zones: `monitor`, `repeat`, or `last`.
- Optional [MeshMapper](https://meshmapper.net/) coverage outside zones:
  Monitor in covered squares, Repeat in uncovered squares. The service
  respects API quotas and keeps working offline from a cached snapshot or the
  configured fallback.
- Hysteresis: a dwell time before Repeat and a minimum Monitor time.
- Monitor applied as OpenHop `monitor` or `no_tx`.
- Manual overrides and a latched **safe mode** (`no_tx`).
- Optional web interface for status, quick actions, configuration editing,
  and a zone map. It works on desktop and mobile browsers.
- Optional MeshCore companion for status reports and authorized remote
  commands.
- Optional diagnostic trace of positions and modes, exportable to GeoJSON.
- systemd notify/watchdog integration, low memory use, and no steady-state
  writes to the SD card.

## Modes

| Term | OpenHop mode | Meaning |
| --- | --- | --- |
| Repeat | `forward` | Repeats traffic. |
| Monitor | `monitor` (default) or `no_tx` | Does not repeat. OpenHop `monitor` still permits local TX such as adverts and companion messages. Set `policy.monitor_mode = "no_tx"` to disable all TX instead. |
| Safe mode | `no_tx` | Latched after faults or **Disable all TX**. Only **Clear** releases it, and only after health checks pass. |

The decision order is: safe mode, manual override, missing or stale GPS
(Monitor), Monitor zone, Repeat zone, MeshMapper (if enabled and valid), and
then `policy.outside`.

## Requirements

- Python 3.11 or newer (Debian Bookworm, Raspberry Pi OS Bookworm or Trixie,
  Fedora 39+, or similar).
- An OpenHop repeater with HTTP API access, by default `http://127.0.0.1:8000`.
- gpsd with a GPS receiver, by default `127.0.0.1:2947`.

Optional dependencies, all from distribution packages where possible:

| Feature | Debian package | pip extra | License |
| --- | --- | --- | --- |
| Polygon zones, MeshMapper | `python3-shapely` | `geo` | BSD-3-Clause (GEOS: LGPL-2.1) |
| Web interface | `python3-flask`, `python3-waitress`, `python3-tomlkit` | `web` | BSD-3-Clause, ZPL-2.1, MIT |
| Map | `libjs-leaflet` | (none) | BSD-2-Clause |
| Companion | not packaged; use a virtual environment | `companion` | MIT |

Missing optional dependencies disable the related feature and are reported
in the logs, `--check-config`, and the web interface. This project does not
bundle any dependencies.

## Installation

### Debian, Raspberry Pi OS, Ubuntu

Download `openhop-geofence_<version>_all.deb` from the
[releases page](https://github.com/ewsandor/openhop-geofence-service/releases).
The package works on every architecture.

```sh
sudo apt install ./openhop-geofence_<version>_all.deb
# or: sudo dpkg -i openhop-geofence_<version>_all.deb && sudo apt-get -f install
```

The Debian package requires the distribution packages for polygons,
MeshMapper, the web interface, and the map, even with
`--no-install-recommends`. It creates the `openhop-geofence` system user and
installs `/etc/openhop-geofence/config.toml` as a Debian conffile. On upgrade,
dpkg preserves local edits and prompts if both the local configuration and
packaged default changed; it does not automatically merge them. A deliberately
deleted conffile stays deleted unless explicitly restored. The systemd unit is
installed **disabled**, so installation never enables RF control.

### Fedora and RPM-based distributions

```sh
sudo dnf install ./openhop-geofence-<version>-1.noarch.rpm
```

Fedora does not package Leaflet. To use the map, download Leaflet 1.x
(`leaflet.js`, `leaflet.css`, and `images/`) into a directory and set
`web.leaflet_dir`.

### Other distributions (pip)

Install the wheel into a virtual environment, then install the systemd unit:

```sh
sudo python3 -m venv --system-site-packages /opt/openhop-geofence
sudo /opt/openhop-geofence/bin/pip install 'openhop_geofence-<version>-py3-none-any.whl[geo,web]'
tar xzf openhop_geofence-<version>.tar.gz   # for packaging/ and config/
sudo sh openhop_geofence-<version>/packaging/install-systemd.sh \
    --python /opt/openhop-geofence/bin/python
```

`install-systemd.sh` is idempotent. It creates the service user,
`/etc/openhop-geofence`, the example configuration (if absent), and the unit.
It does not enable the service.

### Standalone

The service also runs in the foreground without systemd:

```sh
openhop-geofence --config ./config.toml --state-dir ./state \
    --cache-dir ./cache --runtime-dir /run/user/$(id -u)/openhop-geofence
```

## Commissioning

Commission the service off air first:

1. Set OpenHop to `no_tx` and confirm the mode persists across an OpenHop
   restart.
2. Confirm OpenHop's airtime and duty-cycle configuration.
3. Create an OpenHop API key and store it for the service:

   ```sh
   sudo install -m 0640 -g openhop-geofence /dev/null /etc/openhop-geofence/secrets/openhop.key
   sudoedit /etc/openhop-geofence/secrets/openhop.key
   ```

4. Edit `/etc/openhop-geofence/config.toml`. Replace the example zones; they
   are placeholders.
5. Validate the configuration:

   ```sh
   sudo -u openhop-geofence openhop-geofence --check-config
   ```

6. Create a web account. `operator` can use mode controls and edit zones and
   policy; `admin` can also change endpoints, secrets paths, web and companion
   authorization settings, and request restart or reboot:

   ```sh
   sudo openhop-geofence passwd admin
   ```

7. Start the service and watch the log:

   ```sh
   sudo systemctl enable --now openhop-geofence
   journalctl -u openhop-geofence -f
   ```

8. Verify each zone, the outside policy, and GPS-loss behavior with a dummy
   load or with OpenHop TX disabled, before operating on air.

## Configuration

The configuration file is TOML at `/etc/openhop-geofence/config.toml`.
[config/config.toml.example](config/config.toml.example) documents every
option. Main sections:

| Section | Purpose |
| --- | --- |
| `[openhop]` | API URL, key file, timeout |
| `[gpsd]` | Host, port, optional device, staleness |
| `[policy]` | `monitor_mode`, `outside` (`monitor`, `repeat`, `last`), hysteresis |
| `[meshmapper]` | Coverage source, key file, lookback days, request budget |
| `[web]` | Bind address, port (default 8080), map options |
| `[companion]` | Optional MeshCore companion |
| `[maintenance]` | `allow_reboot` |
| `[diagnostics]` | Optional trace recording |
| `[[zones]]` | `shape = "circle"` (`latitude`, `longitude`, `radius_m`) or `shape = "geojson"` (`geometry`: GeoJSON Polygon or MultiPolygon, `[lon, lat]` order) |

Validate with `--check-config` before applying changes with
`sudo systemctl reload openhop-geofence`. Reloading an invalid file latches
safe mode (`no_tx`); fix the file, reload, then Clear. Edits made in the web
interface are validated before they are saved. If the service starts with an
invalid configuration, it latches safe mode, requests `no_tx`, and exits
with status 78; systemd does not restart it.

Secrets are separate files under `/etc/openhop-geofence/secrets/`. They are
never shown or edited through the web interface.

### MeshMapper

MeshMapper is optional. It requires a region or group integration key from
MeshMapper. Store the key in `/etc/openhop-geofence/secrets/meshmapper.key`
and add your authorized region URL(s) to `meshmapper.region_urls`.

- The service downloads the region dataset at most every
  `refresh_seconds` (default 6 h) within `daily_request_budget`, and looks up
  locations locally. It never queries per position.
- Snapshots are cached in `/var/cache/openhop-geofence` and used offline
  until `max_cache_age_seconds`.
- If OpenHop has more than one radio, set `meshmapper.radio_filter`.
- If coverage is unavailable, `policy.outside` decides.

Missing squares mean no recorded measurement, not confirmed lack of
coverage.

## Web interface

The web interface listens on `127.0.0.1:8080` by default. To use it from
another device, set `web.bind` to the Pi's LAN address (or `0.0.0.0`) and
restrict access with a firewall. It uses plain HTTP; use it only on trusted
networks, or behind an SSH tunnel or TLS reverse proxy:

```sh
ssh -L 8080:127.0.0.1:8080 pi@repeater.local
```

The interface shows mode, GPS, OpenHop, MeshMapper, and companion status.
It provides Force Repeat, Force Monitor, Resume, Disable all TX, Clear safe
mode, restart, and reboot, plus zone editing on the map and a raw
configuration editor. `[maintenance]` and disk-trace storage settings can
only be changed by editing the file on the device. Set `web.map_enabled = false` on constrained devices;
map tiles and rendering run in the browser.

### Reboot authorization

Host reboot is disabled by default. To enable it, set
`maintenance.allow_reboot = true` and allow the service user to reboot
through polkit:

```sh
sudo cp /usr/share/doc/openhop-geofence/examples/50-openhop-geofence-reboot.rules \
    /etc/polkit-1/rules.d/
```

## Companion (optional)

A MeshCore companion can report status to channels and accept commands by
direct message. Debian does not package the `meshcore` library, so install
it in a virtual environment that also sees the Debian packages:

```sh
sudo python3 -m venv --system-site-packages /opt/openhop-geofence
sudo /opt/openhop-geofence/bin/pip install meshcore
sudo systemctl edit openhop-geofence
```

In the editor, add:

```ini
[Service]
ExecStart=
ExecStart=/opt/openhop-geofence/bin/python -m openhop_geofence --config /etc/openhop-geofence/config.toml
```

For a serial companion, also add `PrivateDevices=no` and
`SupplementaryGroups=dialout`. BLE companions require BlueZ.

Commands use the prefix `!geofence`, for example `!geofence status`.

| Command | Where | Action |
| --- | --- | --- |
| `help`, `status` | Channel or DM | Command list; concise status |
| `debug` | Authorized DM | Bounded diagnostic status |
| `repeat`, `monitor` | Authorized DM | Force Repeat or Monitor |
| `notx` | Authorized DM | Disable all TX (latches safe mode) |
| `resume` | Authorized DM | Return to automatic control |
| `clear` | Authorized DM | Clear safe mode after health checks |
| `restart`, `reboot` | Administrator DM | Restart the service; reboot the host |

By default (`command_authorization = "admin_keys"`), only full public keys in
`admin_public_keys` may send control commands. An empty list permits no
control. Control commands require synchronized system time (see below).

## Offline operation and time

The service runs without internet access. MeshMapper uses its cached
snapshot until it expires; after that, `policy.outside` applies.

Raspberry Pis have no real-time clock. Until the clock is synchronized
(`timedatectl` shows `System clock synchronized: yes`), the service does
not use cached coverage, rejects companion control commands, and records
trace times as unknown. To synchronize offline from GPS, use chrony with
gpsd:

```sh
sudo apt install chrony
echo 'refclock SHM 0 refid GPS precision 1e-1 offset 0.2 delay 0.2' | \
    sudo tee /etc/chrony/conf.d/gpsd.conf
sudo systemctl restart chrony gpsd
chronyc sources
```

Use only one clock source: if you use chrony, leave OpenHop's GPS time sync
disabled.

## Supporting services

### gpsd

```sh
sudo apt install gpsd gpsd-clients
sudoedit /etc/default/gpsd   # DEVICES="/dev/serial/by-id/<receiver>"
sudo systemctl enable --now gpsd.socket
gpspipe -w -x 5              # expect TPV reports with "mode":2 or 3
```

Use a stable `/dev/serial/by-id/` path. Only gpsd may open the receiver.
For a remote gpsd, set `gpsd.host` and restrict access to port 2947.

### OpenHop location from gpsd

OpenHop reads GPS from a serial device, an NMEA file, or a modem, but not
from gpsd. Because gpsd owns the receiver,
[packaging/gpsd-nmea-bridge.service](packaging/gpsd-nmea-bridge.service)
writes a small, regularly replaced NMEA file on tmpfs (`/run`). Install the
bridge as described in its header, then set OpenHop's `gps.source: file` and
`gps.source_path: /run/gpsd-nmea/nmea.txt`. Verify the result with your
OpenHop release.

In OpenHop, `advertise_gps_location` controls whether adverts use the GPS
position. Keep `persist_gps_fix_to_config: false` to avoid configuration
writes.

## Raspberry Pi tuning

- **Memory:** On a headless Pi, add `gpu_mem=16` to
  `/boot/firmware/config.txt` and reboot. This returns about 48 MiB to Linux
  and disables the camera and hardware video. Check the result with
  `vcgencmd get_mem gpu` and `free -m`.
- **SD card wear:** The service writes to storage only for configuration
  edits, state changes, and coverage refreshes. Keep logs in RAM with journald
  `Storage=volatile` and `RuntimeMaxUse=16M`, or use log2ram. If swap is
  needed, use zram.
- **Power:** Sudden power loss can corrupt the SD card, including OpenHop's
  configuration during a mode change. Use a stable supply; for vehicles, use
  a UPS HAT or ignition-sense shutdown. Check undervoltage with
  `vcgencmd get_throttled` (`0x0` is good).
- **Read-only overlay:** The Raspberry Pi OS overlay filesystem
  (`raspi-config`) protects the card, but configuration edits, state, and
  coverage snapshots are lost at reboot unless stored on a writable mount.

On resource-limited devices, disable tracing, the companion, the map, the web
interface, or MeshMapper, in that order. See the resource estimate in
DESIGN.md section 12.

## Diagnostic trace

Set `diagnostics.enabled = true` to record positions, decisions, and modes.
It records to RAM by default; recording to disk requires `directory`,
`max_disk_bytes`, and `max_age_seconds`, plus a `LogsDirectory=` drop-in.
Export a ZIP from the web interface, then convert it offline:

```sh
openhop-geofence-trace trace.zip -o trace.geojson
openhop-geofence-trace trace.zip --summary
```

Open the GeoJSON in QGIS or [geojson.io](https://geojson.io). The trace
contains location history; handle it as private data.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Stays in Monitor | GPS fix: `gpspipe -w -x 5`; hysteresis countdown in the web status |
| `openhop` fault | API URL and key; `curl -H "X-API-Key: ..." http://127.0.0.1:8000/api/stats` |
| Safe mode latched | Status shows the reason; fix it, then Clear |
| MeshMapper unavailable | Key, region URL, quota, clock synchronization |
| External control conflict | Another tool changed the OpenHop mode; the service applies `no_tx` |
| Service does not start | `journalctl -u openhop-geofence -b`; `openhop-geofence --check-config` |

Exit status 78 means invalid configuration. Use `--log-level DEBUG` for
detailed logs.

## Uninstallation

```sh
sudo apt remove openhop-geofence   # keeps configuration and state
sudo apt purge openhop-geofence    # also removes config.toml, state, and cache
```

Purge keeps files that you added under `/etc/openhop-geofence/secrets`.
Afterwards, set the OpenHop mode as required.

## Development

```sh
PYTHONPATH=src:tests python3 -m unittest discover -s tests
```

Tests use fake gpsd, OpenHop, and MeshMapper servers and never contact a
live radio or the MeshMapper API. See [AGENTS.md](AGENTS.md).

### Preparing a release

Run `python3 packaging/bump-version.py` and enter a new, greater
`MAJOR.MINOR.PATCH` version and a one-line release summary. The Debian
maintainer is offered as the changelog author; you can supply another author.
The script updates the Python version and adds Debian and RPM changelog
entries. Review and commit the changes; it does not create or push a tag.

After merging and passing CI, create and push a matching tag (for example
`v1.2.3`). The release workflow checks the version, reruns CI, and publishes
the packages on GitHub Releases.

## License

MIT; see [LICENSE](LICENSE). Dependencies are installed separately and keep
their own licenses.
