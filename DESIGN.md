# OpenHop Geofence Service Design

Design document. Last updated: 2026-11-24.

## 1. Purpose and scope

A Python service uses gpsd position reports to control an OpenHop MeshCore
repeater. It runs in the foreground or as a systemd service on Debian-based
systems, including Raspberry Pi OS Lite (Trixie) on a Raspberry Pi Zero 2W.
Normal operation does not require internet access or a setup LAN.

### Use case

The primary use case is a mobile repeater (carpeater) that fills coverage
gaps without adding unnecessary retransmissions or airtime where the fixed
network already serves companion nodes.

The initial release covers geofenced mode control, optional MeshMapper
coverage, hysteresis, safe mode, a web interface with map-based zone editing,
optional companion messaging and remote control, Linux packaging, and
operator documentation, including optional location and mode tracing.

Companion provisioning, advertised-location synchronization, offline map
tiles, containers, plugin support, and a JavaScript build toolchain are out
of scope. Headless MeshMapper wardriving, GPIO status and controls, and
non-OpenHop radio adapters are future enhancements.

## 2. Requirements

### Mode control

- Support Repeat and Monitor zones. Monitor takes priority in overlapping
  zones.
- Outside zones, select Monitor, Repeat, or Last mode using
  `policy.outside`. The default is Monitor.
- Optionally use MeshMapper outside zones. When MeshMapper is disabled or its
  data is unavailable, apply `policy.outside`.
- Apply the Monitor policy result as OpenHop `monitor` or `no_tx`, configured
  by `policy.monitor_mode`.
- Treat missing or stale GPS as Monitor. Manual overrides and safe mode take
  priority and cannot be cleared by GPS loss.
- Read the actual OpenHop mode at startup. Last mode holds the last confirmed
  mode; if no mode is known, use Monitor.
- Force Repeat and Force Monitor overrides bypass zones and GPS policy. They
  remain active until Resume automatic or process restart.
- Disable all TX immediately and persistently. It latches safe mode across
  restarts. Clearing safe mode is a separate authenticated action that
  verifies service health.
- On unexpected internal faults or invalid control state, persist safe mode,
  request OpenHop `no_tx`, and report the fault. A bounded failure announcement
  notification may precede the mode request under the conditions in
  section 10. Do not clear the latch automatically.
- Use hysteresis to prevent rapid mode changes. Monitor transitions are
  immediate; Repeat transitions require sustained eligibility.
- Under systemd, use the service watchdog so a stalled controller is
  restarted. Startup selects Monitor until Repeat is eligible.
- Treat wall-clock time as trusted only when the system clock is
  synchronized. The service never sets the system clock.
- The operator remains responsible for regional radio regulations,
  including airtime and duty-cycle limits. The service does not measure or
  enforce airtime or duty cycle.

### Zones and MeshMapper

- Support circle zones by default and optional GeoJSON polygons.
- Monitor zones take precedence over Repeat zones.
- MeshMapper is optional and is enabled by `meshmapper.enabled`. Within a
  verified region, covered squares select Monitor; uncovered squares select
  Repeat. Missing squares mean uncovered only within verified scope.
- Match MeshMapper filters to the repeater radio configuration by default.
  Permit an explicit radio selection when necessary.
- Use a 14-day measurement lookback by default. Cache data locally and respect
  API quotas, rate limits, and offline operation.
- Keep SNR and quality thresholds experimental and disabled by default.

### Companion module

- Provide an optional MeshCore companion client for status reporting and
  remote control when the LAN or web interface is unavailable.
- Use the MeshCore Python library for TCP, BLE, and serial communication.
  The example uses an OpenHop companion over TCP.
- Report to configured channels at a configurable minimum severity:
  `info`, `warning`, or `error`. Limit traffic and suppress repeated reports.
- Permit basic status queries in configured channels. Require direct messages
  for debug queries and control commands.
- Make DM authorization configurable. Recommend an administrator public-key
  allowlist; unrestricted DM control requires explicit configuration.
- Use the same controller actions and health checks as the web interface.
  Companion commands cannot bypass safe mode priority.
- Do not transmit advertisements. Publish the companion public key and
  contact link through the LAN interface.
- A separate companion may continue transmitting while the controlled
  repeater is in `no_tx`. A companion sharing its OpenHop radio is blocked.
- Keep the shared companion receiving DMs in `no_tx`, including an authorized
  `clear` command. Transmit no reports, replies, or RF
  acknowledgements until the clear succeeds and the resulting OpenHop mode
  permits transmission.
- For eligible service faults, permit at most three failure announcement send submissions
  within a configurable grace period before requesting `no_tx`. Do not wait
  for recipient ACKs. Explicit Disable all TX and conditions indicating
  prohibited transmission bypass this grace period.

### Web interface and operation

- Provide an optional responsive web interface for status, controls,
  configuration editing, credentials, and zone management. The packaged
  example binds to all interfaces on port 8080 for authenticated LAN setup.
- Enable browser-side map editing by default. Keep circles as the standard
  zone editor and polygons as an advanced option. The map can be disabled.
- Support standalone execution and systemd on mainstream Linux distributions.
- Provide separate **Restart service** and **Reboot host** actions through
  the web interface and companion DMs. Restrict both to administrators.
  Host reboot is enabled by default and the web action does not require
  confirmation. A local configuration setting can disable reboot. Neither
  action clears safe mode.
- Publish a Debian `Architecture: all` package and portable source and wheel
  distributions. Do not vendor or redistribute dependencies; use OS packages
  or pip to install them.
- Minimize persistent writes. Keep runtime state on RAM-backed filesystems
  where available and write durable state only when it changes.
- Provide optional location and mode tracing, disabled by default. When
  enabled, use a bounded rolling RAM buffer. Advanced disk recording requires
  explicit administrator configuration and limits on total file size and retention
  time.
- Support authenticated trace export, mode-colored map tracks with zones
  and MeshMapper coverage squares, and offline GeoJSON conversion without
  additional service dependencies or network requests.
- Keep the service lightweight enough to run alongside gpsd and OpenHop on a
  Raspberry Pi Zero 2W. Validate resource use on hardware after implementation.
- Provide MIT licensing, a README, this design document, AI contributor
  guidance, systemd setup, and automated tests.

## 3. Integration evidence and constraints

### OpenHop

The protected `main` branch was inspected at commit
[`13eb8b2`](https://github.com/openhop-dev/openhop_repeater/tree/13eb8b2ea8b1cdb4a07ed6e282dc99e3aa8a5a8b).
The API details below are based on the implementation at this commit.
Installed releases may differ.

The implementation provides:

- `GET /api/stats`: current mode is `config.repeater.mode`; live radio settings
  are under `config.radio`, with additional multi-radio information.
- `POST /api/set_mode`, JSON `{"mode": "forward"}`, `"monitor"`, or `"no_tx"`.
- The mode setter updates the shared live configuration and saves it to disk.
  Its successful response includes `success`, `mode`, and `persisted`.
- `forward` repeats and permits local TX.
- `monitor` disables forwarding but still permits local TX, including adverts
  and companions. It is **not** a transmit prohibition.
- `no_tx` disables all TX through OpenHop's mode controls.
- Automation uses an administrator-equivalent token in `X-API-Key`, not a
  Bearer JWT. Default OpenHop web port is 8000.

Read the observed mode before deciding or retrying a write. Require a valid
application response and read back the mode; HTTP 200 alone is not success.
If a write times out, its outcome is unknown until readback. Do not report a
requested mode as the actual mode.

The current setter also writes OpenHop's configuration on each call. Avoid
duplicate writes and rapid switching to limit storage wear.

Sources:
[API implementation](https://github.com/openhop-dev/openhop_repeater/blob/13eb8b2ea8b1cdb4a07ed6e282dc99e3aa8a5a8b/repeater/web/api_endpoints.py),
[runtime statistics](https://github.com/openhop-dev/openhop_repeater/blob/13eb8b2ea8b1cdb4a07ed6e282dc99e3aa8a5a8b/repeater/engine.py),
[authentication](https://docs.openhop.dev/projects/openhop-repeater/security-and-authentication/).

### gpsd

Use its standard TCP JSON watch protocol, normally localhost:2947. gpsd owns
the receiver and multiplexes clients; this service never opens the GPS serial
device, parses NMEA, changes the system clock, or requires hardware privileges.

A persistent socket sends `?WATCH={"enable":true,"json":true};` and processes
newline-delimited reports. Accept finite, in-range latitude/longitude from
TPV reports with a 2D or 3D fix. Missing optional fields are normal, not faults.
An explicit lost-fix report invalidates the position immediately; silence
invalidates it after a configurable timeout. No cached position is treated as
a new fix.

Support host, port, and optional device selection so multiple receivers are
not accidentally mixed. Use monotonic receipt time for live deadlines; check
reported timestamps when available to reject replayed/stale reports.

Source: [gpsd client guide](https://gpsd.io/client-howto.html).

### MeshMapper

MeshMapper coverage is primarily collected through community wardriving with
companion nodes. It records companion-to-network links. Coverage is uneven:
missing squares indicate no recorded measurement, not confirmed lack of RF
coverage. Measurements use varied companions, antennas, and vehicles; SNR
values are indicative rather than calibrated.

The [Coverage API](https://wiki.meshmapper.net/coverage-api/) documents:

- `GET https://meshmapper.net/coverage.php`, preferably with `X-API-Key`.
- Region/group-scoped integration keys, normally 100 coverage requests/day.
- `f_days=14` for the default measurement lookback.
- Exact frequency/bandwidth/SF/coding-rate filters, including `f_radio_freq`.
- Grid bounds (about 300 m squares), dominant coverage type, and dominant
  measurement timestamp.
- Per-square quality fields: average `snr` with `snr_min`/`snr_max` (dB,
  nullable), ping `count`, `effective` (mean ping quality 0–3: BIDIR 3;
  TX/RX/DISC 2; DEAD 1; DROP 0), `status_mask` (all types present), `noise`.
- The API does not provide a per-square repeater or neighbor count. `count`
  is the number of pings. `?include=repeaters` lists region repeaters with
  locations but does not associate them with squares.
- A minimum recommended polling interval of 15 minutes. Unfiltered responses
  come from a 15-minute server cache; filtered responses are built on every
  request.
- Every request that reaches the data counts against the daily quota,
  including cache hits, 304 responses, and 400 filter errors. Counters reset
  at midnight UTC.
- A separate per-IP throttle (about 30 requests per rolling window, then a
  120-second lockout, without `Retry-After`).
- Multi-region keys do not support age/radio filters.

The API does not document location/bounding-box or covered-only/uncovered-only
filters. Download the supported regional dataset at a rate within its limits
and perform location lookups locally. Use only documented endpoints and
filters.

The [Zones API](https://wiki.meshmapper.net/zones-api/) provides authorized
region discovery and boundary GeoJSON. Its read budget is separate from the
coverage budget, normally 1,000 requests/day and 30/minute. A null boundary
means unknown scope, not an empty authorized region.

The coverage response's `bbox` encloses returned measurements; it does not
establish the key's authorized geographic boundary.

Verify API documentation and quota limits before implementation and release.

## 4. Architecture

Use one process with typed data records and a single owner for mode decisions.
The service does not require a message broker, ORM, or service bus.

```text
gpsd TCP stream -----------> latest validated fix ---+
validated config ----------> zones / outside policy -+
optional MeshMapper worker -> in-memory coverage ----+--> controller --> OpenHop API
optional web interface -----> command queue ---------+        |
optional companion worker --> command queue ---------+        |
                                                              v
          web interface, companion, trace recorder <-- status snapshots
```

- The controller evaluates once per second and on relevant commands.
- A GPS reader maintains the last validated report with reconnect backoff.
- An optional MeshMapper worker performs slow downloads without blocking GPS
  safety decisions.
- Web handlers read immutable status snapshots and submit bounded commands.
  They never independently write radio mode.
- An optional companion worker uses one asyncio event loop for library events
  and messaging. It reads status snapshots and submits commands to the same
  controller queue. Network operations do not block mode decisions.
- An optional trace recorder consumes status snapshots and controller events.
  It never blocks or influences mode decisions.
- OpenHop requests have short explicit timeouts. The web interface remains
  responsive while a control operation is pending.
- Each thread has a narrow responsibility; a failed worker is surfaced to the
  controller, not silently restarted with lost state.

Only the controller owns ordinary override state, candidate timers, observed
mode, and transitions. Serialize configuration updates and persistent state.

### Dependencies

| Dependency | Task | Debian Trixie package | License |
| --- | --- | --- | --- |
| Python standard library | CLI, sockets/JSON, HTTP client, clocks, logging, files, circle distance, tests | `python3` | PSF |
| Shapely (recommended) | Polygon validation and containment, MeshMapper square lookup | `python3-shapely` | BSD-3-Clause (GEOS: LGPL-2.1) |
| Flask (optional web) | Web routing, templates, request handling | `python3-flask` | BSD-3-Clause |
| Waitress (optional web) | Production WSGI server | `python3-waitress` | ZPL-2.1 |
| tomlkit (optional web) | Comment-preserving config edits | `python3-tomlkit` | MIT |
| Leaflet (optional map) | Browser map | `libjs-leaflet` | BSD-2-Clause |
| MeshCore Python library (optional companion) | Companion transport, messages, and events | Use a distribution package if available; otherwise a virtual environment | MIT |

Shapely handles polygon validation, containment, and spatial lookup.
Circle zones use the standard-library haversine formula; its spherical-Earth
error is about 0.5% (approximately 5 m at a 1 km radius). GeographicLib is
not used; ellipsoidal precision is unnecessary at zone scale.

Shapely loads only when polygon zones or MeshMapper are configured, so a
circle-only installation without MeshMapper never imports Shapely or NumPy.
Debian packages for Shapely and its dependencies use an estimated 30–40 MB
of disk. Section 12 estimates memory use.

Shapely is installed by default through Debian "Recommends" or the pip `geo`
extra. It can be omitted with `apt install --no-install-recommends` or by
leaving out the extra. When it is absent:

- Circle zones work normally.
- Polygon zones fail `--check-config` with an "install python3-shapely" hint.
- MeshMapper is reported unavailable and `policy.outside` decides.

The unit sets `OPENBLAS_NUM_THREADS=1` so NumPy does not start one idle
math thread per core.

Web dependencies are optional and are not imported when the web interface is
disabled. Avoid pandas, geopandas, projection libraries, Leaflet drawing
plugins, and frontend frameworks. Target Python 3.11+ and dependency versions
available in Debian Bookworm and Trixie. Trixie provides Leaflet 1.7.1.

The optional pip `companion` extra installs `meshcore` from
[meshcore-dev/meshcore_py](https://github.com/meshcore-dev/meshcore_py).
Its upstream dependencies include Bleak, pyserial-asyncio-fast,
pycayennelpp, and PyCryptodome, including when only TCP is used. Let the
library implement framing, transport, message retrieval, and contact handling;
do not reimplement these protocols or invoke a CLI subprocess per message.
The [MeshCore CLI](https://github.com/meshcore-dev/meshcore-cli) remains a setup
and diagnostic tool.

Dependencies are installed by the operating system or pip; this project does
not vendor or redistribute them. Debian packages declare apt dependencies;
wheel and source distributions declare pip dependencies.

License references:
[Shapely](https://github.com/shapely/shapely/blob/main/LICENSE.txt),
[GEOS](https://github.com/libgeos/geos/blob/main/COPYING),
[Flask](https://github.com/pallets/flask/blob/main/LICENSE.txt),
[Waitress](https://github.com/Pylons/waitress/blob/main/LICENSE.txt),
[tomlkit](https://github.com/sdispater/tomlkit/blob/master/LICENSE),
[Leaflet](https://github.com/Leaflet/Leaflet/blob/main/LICENSE),
[MeshCore Python](https://github.com/meshcore-dev/meshcore_py/blob/main/LICENSE).

## 5. Mode policy and safety

### Terms

- **Repeat:** the policy result that enables repeating; applied as OpenHop
  `forward`.
- **Monitor:** the policy result that disables repeating. It applies to
  Monitor zones, covered MeshMapper squares, the Monitor outside policy, GPS
  loss, and recoverable failures. `policy.monitor_mode` maps it to OpenHop
  `monitor` (default) or `no_tx`.
- **OpenHop modes:** `forward`, `monitor`, and `no_tx`.
- **Observed mode:** last successfully read OpenHop mode, with timestamp.
  It becomes unknown/stale when connectivity is lost.
- **Desired mode:** what the policy currently requests; distinct from observed.
- **Safe mode:** persistent latch requiring `no_tx`, with cause and timestamp.
- **Last mode:** hold the most recently confirmed mode, initially read from
  OpenHop. No writes are needed when already in that mode.

Manual controls bypass `monitor_mode`: Force Repeat selects `forward`, Force
Monitor selects `monitor` (local transmissions remain allowed), and Disable
all TX selects `no_tx` and latches safe mode.

### Decision priority

1. Safe mode, including explicit Disable all TX: `no_tx`.
2. Manual Force Repeat or Force Monitor override.
3. Missing/stale GPS: Monitor.
4. Any containing Monitor zone: Monitor.
5. Any containing Repeat zone: Repeat.
6. MeshMapper, when enabled and valid for the current position: covered
   squares select Monitor; uncovered or weakly covered squares select Repeat
   (section 7).
7. Outside policy (`policy.outside`): applies when MeshMapper is disabled or
   its data is unavailable (not configured, offline without valid cache,
   outside verified scope, or system time is untrusted):
   - Monitor: Monitor (default).
   - Repeat: Repeat.
   - Last: hold last confirmed mode; Monitor if none is known.

Zone order does not affect overlap precedence. GPS loss does not release a
manual override or safe mode.

Automatic Monitor mapped to `no_tx` does not latch safe mode. Explicit Disable
all TX and unexpected faults do latch it.

### Hysteresis

- Automatic transitions to Monitor are immediate, including entry into a
  Monitor zone.
- Repeat requires continuous eligibility for `repeat_delay_seconds`
  (default 10 seconds) and at least `minimum_monitor_seconds` (default
  30 seconds) since entering Monitor.
- Any contradictory or invalid fix resets Repeat eligibility.
- Crossing between zones with the same resulting mode causes no API write.
- Explicit `no_tx` and safe mode bypass timers. Ordinary overrides also bypass
  automatic timers, subject to safe mode priority.

Timers use monotonic time and are unaffected by wall-clock adjustments.
Hysteresis also limits writes to OpenHop's configuration, which is persisted
on each mode change.

### Failures and recovery

| Condition | Response |
| --- | --- |
| GPS starting, disconnected, explicit no-fix, or stale | Monitor; reconnect without busy loops. |
| Internet absent, MeshMapper timeout/temporary 5xx/quota exhaustion | Use valid cache; otherwise `policy.outside`. |
| MeshMapper enabled but not configured, unsupported key filters, unknown/out-of-scope region, or response exceeds resource limits | Report MeshMapper unavailable and apply `policy.outside`. |
| MeshMapper disabled | No MeshMapper network activity; `policy.outside` decides outside zones. |
| Controller loop stalls under systemd | Stop watchdog notifications; systemd terminates and restarts the service. Startup reads the OpenHop mode and applies Monitor until Repeat is eligible. |
| OpenHop unreachable | Actual mode unknown; retain intent and retry with backoff. On reconnect, apply Monitor before re-enabling automatic Repeat. An active safe mode instead requires `no_tx`. |
| Unexpected internal exception, invalid active configuration/state, unknown OpenHop mode, or persistent API schema/authentication error | Persist safe mode, attempt `no_tx`, report the fault, and prohibit ordinary overrides. |
| Invalid web configuration or action | Reject with an explicit error; retain the valid running configuration. |
| Incomplete network download | Reject the new cache; retain valid old cache and retry conservatively. Repeated complete-but-invalid responses are an integration fault. |
| Persistence failure | Report the fault prominently, attempt `no_tx`; do not claim a durable latch or successful config save. |

Rate-limit repeated outage messages; log the initial failure and recovery.
Do not clear the safe mode latch automatically when the fault disappears.

Clear safe mode verifies the active configuration, durable-state writability,
OpenHop connectivity and API compatibility, and a fresh GPS fix. MeshMapper
availability is not required because `policy.outside` applies when coverage
is unavailable. The action clears the latch and ordinary override, then
restarts eligibility timers. It fails if any required health check fails.
Resume automatic clears an ordinary override and restarts eligibility
timers, but cannot clear safe mode.

The companion interface uses these same checks. Channel messages cannot
change modes or clear safe mode. An eligible failure announcement notification introduces
only the bounded exception in section 10; it does not release the latch.

The controller is the sole mode authority while active. Unexpected external
mode changes trigger a control-conflict fault and safe mode rather than a
repeated contest between writers.

### Watchdog

Use systemd `Type=notify`, `WatchdogSec=` (initially 30 seconds), and
`NotifyAccess=main`. Send `READY=1` after startup checks and `WATCHDOG=1`
only after a completed controller evaluation, not from an independent timer
thread. Implement the notification protocol with a standard-library Unix
datagram socket. A watchdog timeout counts as a failure for
`Restart=on-failure`. Standalone mode has no watchdog.

### Trusted time

Hysteresis, GPS staleness, and grace periods use monotonic time. Wall-clock
time is required for MeshMapper measurement and cache ages, companion command
freshness, and trace UTC timestamps.

Raspberry Pis have no battery-backed real-time clock by default. After an
offline boot, `fake-hwclock` restores an approximate, earlier time. Treat
wall-clock time as trusted only when the kernel reports clock synchronization,
as shown by `timedatectl` `NTPSynchronized`. systemd-timesyncd or chrony sets
this status. While untrusted, do not use persisted age-sensitive coverage,
reject companion control commands that require freshness checks, and record
trace UTC as unknown. Status queries and zone policy continue.

Document chrony with gpsd as an optional time source for offline operation.
gpsd time without a PPS signal is sufficient for these checks.

### Limits of the safety guarantee

The service is not a hardware interlock. A process crash, host power loss,
network failure, or unavailable OpenHop API can prevent a mode change.
OpenHop `no_tx` is unconfirmed until readback succeeds. A saved latch or
systemd restart does not prove that RF transmission has stopped.

Commission OpenHop in persisted `no_tx` before first start. On graceful
shutdown, request the configured Monitor mode without delaying shutdown
indefinitely; active safe mode continues to require `no_tx`. Process
restart clears ordinary overrides but retains safe mode. Abrupt termination,
including a watchdog timeout, cannot guarantee a final mode change; the
restarted service applies the startup policy.

With failure announcement notifications enabled, an eligible fault can leave the existing
mode active for the configured grace period. Local command acceptance is not
proof of RF transmission or recipient delivery. A separate companion's radio
is outside the controlled OpenHop instance's `no_tx` guarantee.

## 6. Zones and coordinate handling

Circles have a unique ID, label, role, WGS84 center, and radius in meters.
Use the standard-library haversine distance with the mean Earth radius.
Points exactly at the radius are inside. The spherical approximation has
less than 0.5% error; use a small radius margin where a boundary matters.

Advanced zones use GeoJSON Polygon or MultiPolygon coordinates, including
holes, stored inline in the same TOML configuration. Use Shapely validation
and `covers`, so outer boundaries count as inside and hole interiors do not.
Reject invalid/self-intersecting geometry rather than silently repair it.
GeoJSON uses `[longitude, latitude]`; circle forms use separately labeled
latitude and longitude fields.

Shapely uses planar coordinates. Support regional GeoJSON polygons with
straight longitude/latitude edges; reject polar and antimeridian-spanning
polygons. Circles use haversine distance independently of Shapely. If a
MeshMapper region has unsupported geometry, report coverage unavailable and
apply `policy.outside`.

## 7. MeshMapper integration

Only activate the client when MeshMapper is enabled and configured.
Explicit zones always decide first, even when MeshMapper is down.

1. Resolve configured region codes through the authenticated country directory,
   then fetch each region boundary from its official URL. Cache boundaries by
   key identity, country and region codes. A coverage bounding box does not
   establish authorization.
2. Fetch coverage with `f_days=14` and matching radio parameters by default.
   Convert OpenHop frequency and bandwidth values from Hz to the API's
   documented units.
3. Validate and store compact square bounds, type, measurement timestamp,
   and, when experimental thresholds are enabled, `snr` and `effective`.
   Ignore visualization fields. Index squares in a standard-library grid of
   buckets for local lookup; Shapely is used only for region-scope tests.
4. Inside verified scope, classify the square containing the fix:
   - **Covered** (Monitor): dominant type is in `covered_types` and, if
     experimental thresholds are enabled, every configured threshold is met.
   - Weak coverage (Repeat, experimental only): covered type, but below a
     configured threshold.
   - **Uncovered** (Repeat): DEAD/DROP, or no eligible square.
5. Unknown type, invalid bounds, or incompatible data are not silently treated
   as uncovered. Validate before replacing the cache.

An explicit radio filter may override automatic matching. For multi-radio
OpenHop instances, require the operator to select the radio used for coverage
filters (`meshmapper.radio_filter`); without it, MeshMapper reports a
configuration fault and `policy.outside` applies. Mode changes apply to the entire OpenHop instance. Multi-region keys
that do not support age or radio filters are unavailable for this integration;
do not relax filters automatically.

### Experimental weak-coverage thresholds

This feature is experimental and disabled by default. With
`weak_coverage_thresholds = false`, classification uses coverage type only and
ignores threshold values. When enabled, report its experimental status in the
web interface and startup log.

The thresholds identify weakly covered squares where a mobile repeater may
provide fill-in coverage. MeshMapper SNR values are collected from varied
companions and equipment; validate their meaning and suitable thresholds
against field data before enabling this feature.

Settings in `[meshmapper]`, used only when enabled:

- `min_snr_db`: average `snr` of a covered square must be at least this
  value; weaker squares select Repeat.
- `min_effective`: the square's `effective` score (0–3) must be at least this
  value. This catches squares whose dominant ping is BIDIR but which are
  mostly DROP/DEAD.

A missing threshold field does not establish weak coverage; evaluate the
square by its coverage type. MeshMapper does not specify which receiver
reports `snr`, so field validation is required before selecting threshold
values. Enabling this feature without at least one threshold is a
configuration error.

### Caching and rate limits

- Refresh coverage every 6 hours with jitter, never more frequently than
  every 15 minutes.
- Limit coverage requests to 80 per UTC day by default, below the typical
  quota of 100. Other clients using the key also consume this quota.
- Refresh boundaries daily; maximum boundary-cache age is 7 days.
- Maximum coverage-cache age is 24 hours, separate from the 14-day
  measurement lookback.
- Work from an in-memory dataset. After a complete, validated refresh whose
  content differs from the stored snapshot (compared by hash), write one
  compact gzip JSON snapshot atomically to `/var/cache/openhop-geofence` so
  coverage survives an offline reboot. Unchanged refreshes write nothing.
  Never replace valid data with a partial refresh.
  A confirmed empty dataset is distinct from a failed download.
- Keep request accounting and backoff state in the RAM-backed runtime
  directory. The directory survives service restarts, not reboots. After
  reboot, use the snapshot timestamp to avoid refetching fresh data.
- Keep scope, filters, response generation time, successful validation time,
  and credential identity with the cache. Key/scope/radio/filter changes
  invalidate it. Do not store plaintext API keys in cache metadata.
- Expire individual measurements against the current lookback, including
  while offline. Normalize the API's documented seconds/milliseconds variants.
  Do not use region-wide `data_age_seconds` as a cell's freshness.
- A 304 does not rejuvenate old measurements or `generated_at`; validate
  against the cache's independent age limits.
- Respect Retry-After and daily quota resets, and back off failed requests.
  Never retry on a schedule that could trip the per-IP burst throttle.
  Requests are at least 30 seconds apart; failure backoff is capped at
  6 hours. A 400, 401, 403, or 404 response is a MeshMapper configuration
  fault: it is reported and not retried until reload, and it does not latch
  safe mode.
  No coverage request is triggered for each GPS fix or square crossing.
- Request gzip; bound compressed (16 MiB) and decompressed (64 MiB) response
  sizes, row count (100,000 squares), and total stored bytes. Exceeding the budget means unavailable, not uncovered.
- While system time is untrusted (section 5), do not use persisted
  age-sensitive coverage and do not send MeshMapper requests. Apply
  `policy.outside` until time is trusted.

## 8. Configuration and Linux installation

Use `/etc/openhop-geofence/config.toml` and install a commented example.
Validate configuration with `tomllib`; use tomlkit to preserve comments when
the web interface edits the file.

The example below enables MeshMapper, the web interface, and the map.
MeshMapper remains optional. With no key file or configured regions, report it as
unavailable and apply `policy.outside`; this is not a configuration error.

```toml
[openhop]
base_url = "http://127.0.0.1:8000"
api_key_file = "/etc/openhop-geofence/secrets/openhop.key"
timeout_seconds = 2.0

[gpsd]
host = "127.0.0.1"
port = 2947
stale_after_seconds = 10.0
# device = "/dev/serial/by-id/REPLACE_WITH_RECEIVER"

[policy]
monitor_mode = "monitor"   # How Monitor is applied: "monitor" or "no_tx"
outside = "monitor"        # Outside zones, or when MeshMapper is unavailable
repeat_delay_seconds = 10.0
minimum_monitor_seconds = 30.0

[meshmapper]
enabled = true # Set false to disable all MeshMapper requests
api_key_file = "/etc/openhop-geofence/secrets/meshmapper.key"
coverage_url = "https://meshmapper.net/coverage.php"
country = "US"
regions = ["ORD"] # Chicago, IL; replace with regions authorized by your key
lookback_days = 14
covered_types = ["BIDIR", "TX", "RX", "DISC"]
match_radio = true
refresh_seconds = 21600
max_cache_age_seconds = 86400
boundary_refresh_seconds = 86400
max_boundary_age_seconds = 604800
daily_request_budget = 80

# Experimental: select Repeat in weakly covered squares, e.g. for a parked
# repeater providing fill-in coverage at the edge of the fixed network.
weak_coverage_thresholds = false
# min_snr_db = 0.0     # Covered squares with weaker average SNR select Repeat
# min_effective = 2.0  # Covered squares scoring below this (0-3) select Repeat

[web]
enabled = true
bind = "0.0.0.0" # Trusted LAN only; never expose directly to the internet.
port = 8080
# Fresh Debian installs prompt for an admin password in the web interface.
# Or use: sudo openhop-geofence passwd admin
# Other installations require creating an account with the same command.
auth_file = "/etc/openhop-geofence/secrets/web-auth"
allow_config_edit = true
map_enabled = true # Set false to disable maps; forms still work
# leaflet_dir = "/usr/share/javascript/leaflet" # From Debian libjs-leaflet

[companion]
enabled = false
transport = "tcp" # "tcp", "ble", or "serial"
host = "127.0.0.1"
port = 5000 # Match the configured OpenHop companion TCP port
openhop_companion_name = "RepeaterCompanion"
shares_openhop_radio = true
report_channels = [0] # Existing companion channel indexes; [] disables reports
report_level = "warning" # "info", "warning", or "error"
outage_warning_after_seconds = 300
minimum_report_interval_seconds = 60
command_prefix = "!geofence"
command_authorization = "admin_keys" # Or explicitly "any_sender"
admin_public_keys = [] # Full public keys; empty list permits no control
# Send a final fault message before disabling TX, when safe and possible.
announce_failure_enabled = true
announce_failure_max_attempts = 3 # Total submissions across report channels
announce_failure_grace_seconds = 10.0 # Initial value; validate against airtime
# serial_device = "/dev/serial/by-id/REPLACE_WITH_COMPANION"
# serial_baud = 115200
# ble_address = "REPLACE_WITH_COMPANION_ADDRESS"
ble_pin_file = "/etc/openhop-geofence/secrets/companion-ble-pin"

[maintenance]
allow_reboot = true # Set false to disable host reboot requests

[diagnostics]
enabled = false
storage = "ram" # "disk" requires explicit configuration
sample_interval_seconds = 5.0
max_buffer_bytes = 4194304 # 4 MiB rolling record buffer
max_context_bytes = 8388608 # 8 MiB zone and coverage context
# Disk recording requires all three settings below:
# directory = "/var/lib/openhop-geofence/trace"
# max_disk_bytes = 33554432 # 32 MiB total, including context
# max_age_seconds = 86400 # Retain at most 24 hours

# Repeat [[zones]] for each zone; IDs must be unique.
[[zones]]
id = "chicago-repeat"
name = "Chicago repeat area"
role = "repeat"
shape = "circle"
latitude = 41.8781
longitude = -87.6298
radius_m = 1000.0

[[zones]]
id = "chicago-monitor"
name = "Chicago monitor exclusion"
role = "monitor"
shape = "circle"
latitude = 41.8781
longitude = -87.6298
radius_m = 100.0
```

The example coordinates are illustrative. Polygon zones use
`shape = "geojson"` and a `geometry` inline table containing GeoJSON type and
coordinates. Store all zones in the same file for atomic updates.

Validate types, unique zone IDs, ranges, URLs, modes, timers, and unknown
keys. Reject invalid or unknown settings. Missing optional MeshMapper
credentials or region codes make the feature unavailable; apply
`policy.outside`. Store secrets separately in restricted service-managed files;
administrators can replace but not read their values through the web interface.

The installer creates an unprivileged `openhop-geofence` account:

- Root-owned executable/package and systemd unit.
- Config directory `/etc/openhop-geofence` mode 2775 `root:openhop-geofence`
  (setgid): the service can atomically replace `config.toml`, which it owns
  (0664). Members of the service group can replace entries in this directory,
  so do not put unrelated root-managed files there. Other users can list the
  directory and read the configuration, but only root and the service group
  can edit it. Atomic writes preserve the
  replaced file's owner and group; new files take the directory's group.
  Debian manages `config.toml` as a conffile, including changes made by the
  service; RPM installs it as `%config(noreplace)`.
- Service-owned secrets directory, listable by local users but not writable
  by the service group. Files are mode 0640 for service-group access;
  the directory is mode 2755. Administrators must restrict existing secret
  files before making the directory listable.
- Durable state (safe mode latch and one-time setup marker) under `/var/lib/openhop-geofence`.
- Coverage snapshot under `/var/cache/openhop-geofence`.
- Runtime lock, status, and MeshMapper request accounting under the
  RAM-backed `/run/openhop-geofence` (systemd `RuntimeDirectory=` with
  `RuntimeDirectoryPreserve=yes`, so it survives service restarts).
- Logs to stdout/stderr, captured by journald under systemd.

### Persistent storage

Minimize writes to SD and eMMC storage, particularly on battery-powered
systems. With disk tracing disabled, steady-state operation performs no
writes to persistent storage. Durable writes are otherwise event-driven:

| Data | Location | When written |
| --- | --- | --- |
| Live fix, status, timers, overrides | Process memory | Never written to disk |
| Diagnostic trace and context, when enabled | Bounded process memory | RAM only unless disk recording is explicitly configured |
| Companion reports, command deduplication, and pending replies | Process memory | Never written to disk |
| Lock, request accounting, backoff | `/run` (tmpfs) | RAM only |
| Web login sessions | Signed cookie, in-memory key | Never; logins reset on restart |
| Safe mode latch | `/var/lib` | Only when set or cleared |
| Coverage snapshot | `/var/cache` | Only when validated content changes (at most a few times/day) |
| Config file | `/etc` | Only on explicit user save |
| OpenHop mode (OpenHop's own config) | OpenHop | Only on real mode transitions, limited by hysteresis |
| Logs | journald | Transitions, faults, recoveries; repeated outages rate-limited |

- No periodic heartbeat/status files, no per-fix logging at default level,
  no SQLite. Explicit disk tracing is an exception to the persistent-write
  policy; its limits and storage wear must be documented.
- Byte-compile at installation (section 12) and set
  `PYTHONDONTWRITEBYTECODE=1` in the unit so the service never writes `.pyc`
  files.
- Status is served from memory; web polling performs no disk I/O.
- Document journald options for read-mostly systems, including
  `Storage=volatile`, a small `SystemMaxUse=`, and log2ram. Do not change
  system-wide logging policy during installation.
- The gpsd-to-OpenHop location bridge (section 11) writes to `/run`, never
  to the SD card.

For standalone use, provide configurable config, state, cache, and runtime
paths. Provide a console entry point
`openhop-geofence` and `python -m openhop_geofence`, plus `--check-config`.

The systemd unit uses a dedicated account, `Restart=on-failure`, the
watchdog settings in section 5, filesystem protection, `NoNewPrivileges`,
and explicit writable directories. The service
requires no root, GPIO, serial-device, or `CAP_SYS_TIME` privileges. Order
startup after `network.target`; do not require `network-online.target`,
internet access, or a local gpsd service for remote gpsd. Retry unavailable
dependencies in the application. Provide an idempotent privileged installer
that preserves existing configuration and does not enable RF.

Web saves validate the complete candidate and verify its configuration
revision. Write to a temporary file in the same directory, fsync it, atomically
replace the configuration, then fsync the directory. Apply changes only after
the save succeeds. Serialize saves with reloads and concurrent edits, preserve
comments and permissions, and reject stale edits with a conflict response.

On SIGHUP or an explicit reload, validate the file and reset eligibility
timers. Invalid active configuration raises a visible fault and latches safe
mode. An invalid configuration at startup latches safe mode, attempts
`no_tx`, and exits with status 78, which the unit lists in
`RestartPreventExitStatus=` so systemd does not restart it. Changes to bind, authentication, or map-serving settings require a
service restart; report this when saving.

### Service restart and host reboot

Both interfaces submit the same maintenance requests to the controller.
Accept only one pending request and log the requester, action, and outcome.
Use graceful shutdown with bounded cleanup. Preserve the safe mode latch
and do not lift `no_tx` to complete maintenance or send a reply. Restart
clears ordinary overrides and runs the normal startup checks.

**Restart service** restarts only the geofence process. Under systemd, use a
dedicated exit status (75) listed in `RestartForceExitStatus=`; in standalone mode,
re-execute the service with its original arguments after cleanup. The service
does not need permission to manage other units.

**Reboot host** requests an orderly reboot of the Linux host running the
geofence service, not a remote OpenHop or companion device. The local
configuration setting `maintenance.allow_reboot` defaults to `true` and
can be changed by a web administrator. When enabled, use the local OS
reboot interface with narrowly scoped authorization (systemd-logind and
polkit): `systemctl reboot --no-wall --no-ask-password`, authorized by the
installed polkit rule `packaging/50-openhop-geofence-reboot.rules`. This
rule allows the service account itself to reboot, not only requests through
the interfaces. Do not grant the service root access or bypass shutdown
inhibitors. If reboot is disabled, unsupported,
or denied, report the reason and continue running. Acceptance means a request
was scheduled, not that the service or host has returned.

## 9. Lightweight web interface

Use Flask templates, local CSS/JavaScript, and Waitress with a bounded worker
pool. Do not use Flask's development server in service mode.

Display:

- Automatic/override/safe mode state and reason.
- Observed OpenHop mode, age, desired mode, and pending/error state.
- GPS fix quality, age, location, and matched zone names.
- Hysteresis countdown.
- MeshMapper enabled/available state, coverage result, measurement/cache ages,
  quota/backoff, and when `policy.outside` applies.
- Configuration revision and any restart-required changes.
- Companion connection, reporting level, authorization policy, last send
  result, and any pending failure announcement deadline.
- Diagnostic recording state, retention limits, dropped records, and export
  availability.

Provide Force Repeat, Force Monitor, Disable all TX, Resume automatic, Clear
safe mode, reload, and validated configuration editing. Persist the safe mode
latch before requesting `no_tx`; report persistence and OpenHop confirmation
separately.

Provide separate **Restart service** and **Reboot host** controls in the
authenticated administration interface. Reboot requests are submitted
without a confirmation prompt.

Use authenticated same-origin session cookies, password hashes via Werkzeug,
CSRF protection for mutations (`X-CSRF-Token` header), origin and host checks, bounded request bodies,
and bounded login attempts. Require authentication on localhost except for
the one-time administrator setup.
Accounts are stored one per line as `user:role:hash` in the service-managed
`web.auth_file`, created with `openhop-geofence passwd USER [--role
admin|operator]`. A fresh Debian install with no account file creates a durable
one-time setup marker, not a default account or password. Anyone who can
reach the service may claim setup and create `admin`; claim the marker before
creating the account, and never overwrite an existing account file or reopen
setup when the file is deleted. If setup is interrupted after the claim,
recover using the local `passwd` command. Upgrades never create a setup marker.
For older installations using the former `geofence` default, require a
password change before other actions. Invalidate sessions after a password change.
Allow administrators to replace OpenHop and MeshMapper API keys and the
companion BLE PIN without revealing saved values; accept only non-symlink
files directly in the managed secrets directory. Other installation methods
require manual account creation.
Both roles may use mode controls, export traces, and edit
configuration; endpoints, secret paths, the `[web]` section, and companion
authorization require an administrator, and maintenance requests and changes
to `[maintenance]` and disk-trace storage settings require an administrator.
The cookie signing key is generated in memory at startup. The raw
configuration editor returns 409 Conflict on a revision mismatch; zone edits
are applied to the TOML document with tomlkit. Use POST for
mode, configuration, and maintenance actions. Do not expose upstream tokens,
arbitrary file paths, or proxy requests. Restrict changes to API endpoints,
secret paths, companion authorization, and administrator keys to
administrators.

Use configurable port 8080 by default; it does not conflict with OpenHop's
default port 8000 or gpsd's 2947. Report binding failures explicitly. Web
server failure must not stop mode control; report the unavailable interface
through logs and the CLI. Do not select another port automatically.

The packaged example binds to all interfaces for LAN setup. Use a trusted
setup network, restrict access with a firewall, and immediately complete
initial setup; another LAN user could claim it first. The interface is not hardened for
direct internet exposure, which is not recommended. For remote access, harden
the host and network and use a trusted VPN or SSH tunnel; a TLS reverse proxy
alone does not harden the application. HTTP authentication does not encrypt
traffic.

Use a responsive layout with large controls for desktop and mobile browsers.
Poll status every five seconds while the page is visible. The control loop
does not depend on a browser or map.

When maps are enabled:

- The service serves Leaflet's static files (about 150 KB, cached by the
  browser) and zone data as JSON. The browser requests map tiles directly
  from the tile provider and performs rendering and editing.
- Serve Leaflet from the OS-installed `libjs-leaflet` (or configured
  `leaflet_dir`), never from a runtime CDN and never copied into this
  project. If it is missing, show an install hint; forms still work.
  Fedora does not package Leaflet; the operator downloads it and sets
  `web.leaflet_dir`.
- The tile URL and attribution are configurable (`web.tile_url`,
  `web.tile_attribution`). Current coverage squares near the position are
  served by `/api/coverage`.
- Use `strict-origin-when-cross-origin` referrer policy so HTTPS tile requests
  receive the page origin, as required by the default OSM tile service,
  without disclosing page paths or query strings cross-origin.
- Use attributed OpenStreetMap-compatible tiles from a configurable provider
  permitted for this use; follow its tile policy and display attribution.
- Do not bulk-download public tiles or promise offline basemaps.
- Use a small project script with core Leaflet APIs; the server validates
  submitted zones.
- Circles use center/radius controls; polygon editing is explicitly advanced.
- Offline, zone lists/forms and status still work; missing tiles produce a
  clear notice. Disabling maps causes no map/tile requests.

Tile requests reveal the viewed area to the provider. Explain this and
MeshMapper location/data privacy in the README.

### Diagnostic location and mode trace

Recording is disabled by default and is independent of ordinary logs and
the web interface. When enabled, sample valid GPS positions every five
seconds by default and record mode transitions, GPS loss/recovery, overrides,
safe mode changes, and control errors immediately. Record UTC when known,
monotonic elapsed time, a process-session identifier, fix validity and age,
policy result and reason, desired mode, confirmed OpenHop mode and its age,
and pending or failed control requests. Never label a requested mode as
confirmed or reuse a stale fix as a current location.

Store versioned JSON Lines records in a rolling buffer, bounded to 4 MiB by
default. Retain zone definitions and relevant MeshMapper squares used for
recorded decisions in a separate context budget, initially 8 MiB. Reference
configuration and coverage revisions, filters, fetch times, and measurement
ages. Reuse already available data; tracing must not trigger API requests.
Exclude credentials, companion identities, and unrelated configuration.
Evict old records and unreferenced context within the budgets; mark missing
or oversized context explicitly rather than substituting current data.
RAM recording is lost on process restart or host reboot.

Advanced disk recording requires `diagnostics.storage = "disk"`, a writable
directory, and positive total-byte and retention-time limits. Web administrators
may configure these settings. Use bounded
rotating files and prune the oldest data when either limit is reached,
including after restart. Include context and temporary output in storage
budgets. Use a bounded writer queue and batched writes; do not fsync each GPS
sample. A crash may lose recent buffered records. Full storage, permission
errors, and queue overflow produce visible diagnostic errors and dropped
record counts without blocking mode control or independently latching safe
mode. Recording adds persistent writes only when explicitly configured;
installation must not enable it.

The authenticated web interface provides recording controls, clear, and
download actions within the locally configured storage policy. Show tracks
colored by confirmed `forward`, `monitor`, and `no_tx`, with unknown or stale
mode observations marked separately. Split segments at mode changes, GPS
gaps, and process-session boundaries; do not interpolate through missing
positions. Overlay configured zones and recorded MeshMapper coverage context.
Distinguish historical context from current layers and show unavailable
context explicitly. These tracks are diagnostic samples, not precise
transition locations or evidence of actual RF transmission.

Export a bounded ZIP archive containing JSON Lines records, a schema/version
manifest, and GeoJSON zone and coverage context. Provide a standard-library
CLI converter to produce mode-segmented GeoJSON tracks and event points for
QGIS or other GeoJSON viewers, without the running service, gpsd, Shapely, or
internet access. Conversion and browser rendering preserve gaps and context
revisions. Bound export memory and work so downloads do not stall control.
Leaflet can display tracks and exported context without basemap tiles;
offline basemap downloads remain out of scope.

Location history is sensitive. Require authentication for trace access and
restrict disk permissions to the service and administrators. Do not include
traces in companion reports or automatically upload them. Document retention,
RAM loss, explicit disk recording, manual exports, and deletion in the README.

## 10. Companion module

### Connection and identity

Use the MIT-licensed [MeshCore Python library](https://github.com/meshcore-dev/meshcore_py)
with a persistent connection. Its documented transport constructors support
TCP, BLE, and serial; automatic message fetching and events provide incoming
channel messages and DMs. Retry connection failures with backoff and report
them locally. A companion outage does not stop the controller or change the
repeater's mode.

The example connects to an OpenHop companion on localhost:5000. Select a
dedicated companion and match its configured port; do not share the connection
with another companion client. OpenHop's companion frame server accepts one
active client per port.

Use library self-information and contact export for the public key and
contact URI. For OpenHop, its authenticated `GET /api/companion/` also returns
configured companion names and public keys. Verify that the selected name
matches the connected identity. Display the public key and an importable
contact link in the LAN interface without exporting private keys or sending
an advertisement.

BLE requires the distribution's BlueZ service and appropriate user
permissions. Serial requires access to the selected device; use a stable
device path. Apply only the permissions needed by the selected transport.
TCP has no additional hardware-service requirements. Companion channels and
contacts are provisioned separately; this module does not change radio
settings, channel keys, identities, or advertisement schedules.
Setup instructions must disable companion advertisements in OpenHop or the
companion firmware and provision administrator contacts without relying on
advertisement discovery.

Future headless MeshMapper wardriving may use this same companion, sharing the
existing library connection and events with reporting and commands. Keep a
single connection owner; do not add a second client to the companion port.
Wardriving is outside the initial release.

Sources:
[library transports and events](https://github.com/meshcore-dev/meshcore_py/blob/main/README.md),
[OpenHop companion API](https://github.com/openhop-dev/openhop_repeater/blob/13eb8b2ea8b1cdb4a07ed6e282dc99e3aa8a5a8b/repeater/web/companion_endpoints.py),
[OpenHop companion configuration](https://github.com/openhop-dev/openhop_repeater/blob/13eb8b2ea8b1cdb4a07ed6e282dc99e3aa8a5a8b/config.yaml.example).

### Reporting

`report_channels` selects existing companion channel indexes. An empty list
disables automatic reports without disabling DM control. `report_level`
sets the minimum severity independently of local logging:

| Level | Reports |
| --- | --- |
| `info` | Confirmed mode changes, overrides, recovery, and service state changes |
| `warning` | Extended GPS or MeshMapper unavailability after the configured delay |
| `error` | Faults, failed mode requests, and entry into `no_tx` or safe mode |

Warn about MeshMapper only when enabled coverage is unavailable, not merely
when internet is absent and valid cached coverage remains usable. Include
the applied outside policy. Report desired and observed modes separately.
Channel reports omit credentials, detailed location, and raw configuration.

Coalesce repeated events, prioritize errors, bound the in-memory queue, and
expire obsolete reports. Rate-limit normal reports and command replies; do
not send periodic heartbeats by default. On reconnect, report the current
state rather than replaying the outage history. Automatic-report filtering
does not suppress an explicitly requested status reply.

Use the library's documented text limits and concise messages. Local send
acceptance does not establish recipient delivery. Channel messages do not
provide recipient ACKs. Reporting errors remain visible in local logs and
status; do not recursively report a send failure through the same connection.

### Commands and authorization

Use a configurable command prefix, defaulting to `!geofence`, and a fixed
command grammar:

| Command | Interface | Action |
| --- | --- | --- |
| `help` | Configured channel or DM | List available commands |
| `status` | Configured channel or DM | Return concise mode, GPS, and coverage status |
| `debug` | Authorized DM | Return bounded diagnostic status without secrets |
| `repeat` | Authorized DM | Force Repeat |
| `monitor` | Authorized DM | Force OpenHop `monitor` |
| `notx` | Authorized DM | Disable all TX: latch safe mode and request `no_tx` |
| `resume` | Authorized DM | Clear an ordinary override and resume automatic control |
| `clear` | Authorized DM | Clear safe mode after controller health checks |
| `restart` | Administrator DM | Restart the geofence service only |
| `reboot` | Administrator DM | Request a reboot of the geofence host |

All commands that change state require DMs. Channel membership and display
names are not individual authentication. The default `admin_keys` policy
accepts control only from configured full administrator public keys. Resolve
the received sender prefix against known full keys; reject unknown or
ambiguous identities. An empty administrator list permits no control.
`any_sender` explicitly permits control from any DM sender and must display a
warning in configuration checks and the web interface.

Maintenance commands always require an allowlisted administrator public key,
even under `any_sender`. A reboot requires `maintenance.allow_reboot`, an
administrator key, and a fresh, non-duplicate message. Because a blocked
shared radio cannot send a confirmation prompt, the DM command does not use
a second confirmation step.

Ignore the companion's own messages and reject unsolicited command formats.
Bound replies, parsing work, and command rate. Deduplicate messages and use
explicit mode-setting commands rather than state-flipping toggles. Reject
stale or pre-start control messages from companion backlogs. If message age
cannot be validated because system time is untrusted, permit status queries
but reject control with an explicit error. This includes `clear`; configure
chrony with gpsd (section 5) to permit remote recovery without internet
access.

Return controller results rather than assuming that a queued request
succeeded. `resume` cannot clear safe mode; `clear` uses the same
health checks as the web interface. Do not expose shell execution, arbitrary
configuration editing, or MeshCore CLI passthrough.

### Shared-radio `no_tx` and failure announcement notifications

Set `shares_openhop_radio` explicitly. A separate companion may continue to
report and receive commands while the controlled repeater is in `no_tx`.
On a shared OpenHop instance, `no_tx` blocks companion transmissions but does
not prevent receiving a DM to clear safe mode.

Keep the companion connection and incoming-message processing active in
`no_tx`. Authenticate and validate `clear` before submitting it to
the controller. Receiving a command alone does not release safe mode: the
clear must pass the controller's health checks.

While the shared radio is in `no_tx`, suppress all outgoing reports, command
replies, and RF acknowledgements, including rejection or failure messages.
Record results locally instead. Do not queue these messages for later
transmission. A clear confirmation may be sent only after the latch is
cleared and readback confirms a mode that permits TX. If automatic policy
still selects `no_tx`, remain receive-only. Do not change the mode merely to
acknowledge the clear. This restriction does not apply to a separate
companion radio.

For an eligible service fault:

1. Persist the safe mode latch and publish the fault locally.
2. If failure announcement reporting is enabled, report channels are configured, the
   shared companion is connected, and the observed repeater mode permits
   transmission (`forward` or `monitor`), allow a failure announcement report within
   `announce_failure_grace_seconds`.
3. Submit at most `announce_failure_max_attempts` messages across configured report
   channels (default and maximum three total). Do not wait for recipient ACKs.
4. Request `no_tx` no later than the grace deadline, regardless of connection
   state, message acceptance, or queue progress.

The initial grace period is 10 seconds and requires field validation against
packet airtime and OpenHop's transmit scheduling. Allow nominal queue-drain
time within this limit. The portable library does not provide a universal
RF transmit-queue-drained guarantee: channel sends return local acceptance,
and repeated submissions may be deduplicated. Three submissions do not prove
three RF transmissions or successful delivery.

Do not delay explicit Disable all TX, routine Monitor decisions mapped to
`no_tx`, or faults indicating prohibited transmission. If the fault prevents
safe validation of the notification path, request `no_tx` immediately. An
explicit Disable all TX cancels any pending grace period. Never re-enable TX
to send a report or extend the deadline for reconnects or retries. Do not
replay failure announcement attempts after restart. A saved latch requires immediate
`no_tx` on startup.

Keep the grace deadline in the controller and messaging in the companion
worker. The wait must not block GPS processing or other control actions.
Report `no_tx` as desired but unconfirmed until mode readback succeeds.
If persistence fails, attempt `no_tx` immediately without a notification
delay.

## 11. Project structure and documentation

Repository structure:

```text
pyproject.toml
LICENSE
README.md
DESIGN.md
AGENTS.md
CLAUDE.md
src/openhop_geofence/
    __init__.py
    __main__.py
    cli.py              entry point, signals, exit codes, passwd
    config.py           TOML loading and validation
    policy.py           decision priority and hysteresis (pure functions)
    controller.py       control loop, commands, safe mode, status
    zones.py
    gpsd.py
    openhop.py
    meshmapper.py
    state.py            atomic writes and persisted latches
    system.py           sd_notify, clock synchronization, reboot request
    companion.py
    web.py
    trace.py            diagnostic recorder
    trace_convert.py    offline GeoJSON converter (openhop-geofence-trace)
    templates/
    static/
tests/                  unittest suite with fake servers (tests/fakes.py)
config/config.toml.example
packaging/openhop-geofence.service
packaging/install-systemd.sh
packaging/check-version.sh
packaging/50-openhop-geofence-reboot.rules   installed polkit rule
packaging/gpsd-nmea-bridge.service           example gpsd-to-OpenHop bridge
packaging/fedora/                            RPM spec and sysusers.d file
debian/
.github/workflows/ci.yml        pull-request checks and package builds
.github/workflows/release.yml   tagged releases
.github/scripts/                container build and smoke-test scripts
```

Organize modules by responsibility. Use type annotations and docstrings for
public interfaces. Document safety policy, API behavior, units, and
concurrency. Avoid redundant comments.

The README covers the use case, features, mode distinctions, supported
platforms, installation, safe off-air commissioning, configuration, web access,
systemd, companion setup and authorization, dependency licensing, offline
operation, time synchronization, diagnostic recording and offline trace
viewing, troubleshooting, and uninstallation that preserves operator data.

The README states that the operator is responsible for regional radio
regulations, including airtime and duty-cycle limits for repeating, companion
reports, and failure announcement messages. The service does not measure or enforce
airtime or duty cycle. The README recommends confirming OpenHop's airtime and
duty-cycle configuration before operation, and refers to OpenHop's
documentation and local regulations for specific settings.

The README also recommends setting `gpu_mem=16` on headless Raspberry Pis in
`/boot/firmware/config.txt` to return about 48 MiB of the default GPU
reservation to Linux. This disables camera and hardware-video support.
Document verification with `vcgencmd get_mem gpu` and `free -m`. The installer
must not modify boot configuration.

### Power and storage reliability

Sudden power loss or undervoltage can corrupt SD storage, including
OpenHop's configuration while a mode change is being saved. Document
power-supply guidance for battery and vehicle installations, including a
UPS HAT or ignition-sense input that initiates a clean shutdown, and
undervoltage checks with `vcgencmd get_throttled`. Document the optional
Raspberry Pi OS read-only overlay filesystem (`raspi-config`) and its
effect: persistent state, configuration edits, coverage snapshots, and disk
traces do not survive a reboot unless stored on a writable mount. The
service does not implement power management or configure the overlay.

`AGENTS.md` documents safety invariants, architecture, validation commands,
integration fixtures, troubleshooting, and the requirement to test without
live radio access. `CLAUDE.md` points to `AGENTS.md`.

### gpsd and OpenHop setup

Document local and remote gpsd setup, receiver permissions, stable device
paths, device selection, network access controls, and diagnostics such as
`gpspipe -w -x 5`. Follow the installed distribution's gpsd unit and socket
conventions.

The OpenHop configuration supports serial, file, and modem HTTP
GPS sources, but not gpsd directly. Document an independently operated
gpsd-to-NMEA/file bridge for `gps.source: file`, including bounded output on
tmpfs (`/run`), stale-data handling, service permissions, and reconnection.
Do not store bridge data on persistent media. `gpspipe -r` provides NMEA
output but does not by itself provide a bounded rolling file. Verify the
bridge against the installed OpenHop file reader.

Document `advertise_gps_location` and the effect of
`persist_gps_fix_to_config`; disabling persistence reduces flash writes.
Verify advertised location separately from the service's gpsd fix. Only one
process may own or configure the physical receiver. The bridge is separate
from the geofence control loop.

Source: [main GPS example](https://github.com/openhop-dev/openhop_repeater/blob/13eb8b2ea8b1cdb4a07ed6e282dc99e3aa8a5a8b/config.yaml.example)
and [gpspipe](https://gpsd.io/gpspipe.html).

## 12. Packaging, CI, and validation

Each tagged release publishes:

- `openhop-geofence_<version>_all.deb`: one Debian package for every Debian
  architecture (amd64, arm64, armhf, and others). The service is pure Python,
  so use `Architecture: all`. Install with
  `sudo dpkg -i openhop-geofence_<version>_all.deb`. Because `dpkg` does not
  download dependencies, follow with `sudo apt-get -f install`, or use
  `sudo apt install ./openhop-geofence_<version>_all.deb` to install the
  package and dependencies together. The Debian package installs the
  dependencies for its default MeshMapper, web, and map configuration even
  with `--no-install-recommends`.
- `openhop-geofence-<version>-1.fc<N>.noarch.rpm`: Fedora package built on
  the current Fedora release, with a sysusers.d account and the same unit.
  It depends on that release's Python version; other RPM distributions can
  rebuild the included source RPM or use the wheel.
- Portable project files: source tarball (sdist) and wheel for pip/pipx or
  manual installs on non-Debian distributions. The sdist includes
  `install-systemd.sh`, the unit, and the example configuration.
- `SHA256SUMS` for all artifacts.

Declare Debian dependencies from distribution repositories; do not bundle
them:

- Depends: `python3 (>= 3.11)`, `python3-shapely` (polygons, MeshMapper), `python3-flask`,
  `python3-waitress`, `python3-tomlkit` (web interface), `libjs-leaflet` (map).
- Suggests: `gpsd`, `gpsd-clients`, `python3-venv` (optional companion).

Report missing optional features (web interface, map, polygons, MeshMapper, or
companion messaging) without failing on other installation methods.

For a fresh Debian install, persist a commissioning safe-mode latch before
enabling and starting the service; it must not automatically enable RF.
When creating the one-time setup marker, print the configured web port and
available local addresses or an actionable fallback if none are available.
OpenHop must already be configured as `no_tx` because the service cannot
control it before API credentials are supplied. Manage the Debian configuration as a dpkg conffile so local edits
receive the standard upgrade choices. Byte-compile with `py3compile`
for the installed Python version; do not ship `.pyc` files.
Use `debhelper`, `dh-python`, and `pybuild`. Do not install packages into
Debian's externally managed system Python with pip.

If `meshcore` is unavailable from the distribution, install it in a root-owned
virtual environment with access to distribution site packages, and configure
systemd to run `python -m openhop_geofence` using that interpreter. This allows
the Debian-installed service and its optional dependencies to share one
process without a system-wide pip installation. The standalone pip install
uses the `companion` extra. Do not vendor dependencies or silently install
them at service startup.

GitHub Actions:

- Run stdlib unittest and bytecode compilation on supported Python versions.
- Validate the example config and offline/no-web startup.
- Build sdist, wheel, and the `.deb` in a Debian Trixie container.
- Smoke-test `dpkg -i` + `apt-get -f install` in clean Debian Bookworm and
  Trixie containers on amd64 and arm64 (native runners) and armhf (QEMU),
  checking imports, console entry points, Shapely loading, and unit syntax.
- Pin third-party actions to reviewed commits and use minimum permissions.
- Publish release artifacts only from approved tags.

Release process:

1. Update `__version__` in `src/openhop_geofence/__init__.py` (the single
   source for Python metadata), add a `debian/changelog` entry
   (`dch -v X.Y.Z`), and update `Version:` and `%changelog` in the RPM spec.
   `packaging/check-version.sh` fails CI on any mismatch.
2. Merge to `main`, then push an annotated tag `vX.Y.Z`.
3. `release.yml` checks the tag against the version, runs the full CI
   workflow, and attaches the sdist, wheel, `.deb`, `.rpm`, source RPM, and
   `SHA256SUMS` to a GitHub Release with generated notes.

`ci.yml` runs on pull requests and is intended as a required status check.

Install Shapely's native GEOS and NumPy dependencies from the distribution.
Verify availability on armhf; do not require building GEOS or NumPy on a
Pi Zero.

Focused offline tests, using fake gpsd/OpenHop/MeshMapper servers and a
controllable clock, must cover:

1. Zone overlap, circle boundary (haversine), polygon holes,
   invalid geometry/coordinates.
2. The full decision priority, including MeshMapper between zones and the
   outside policy, every `policy.outside` value with MeshMapper disabled and
   as its fallback when coverage is unavailable, and
   `monitor_mode` mapping.
3. GPS startup, explicit fix loss, stale data, reconnect, and that GPS loss
   never releases a manual override or safe mode.
4. Repeat dwell/cooldown under boundary jitter and wall-clock changes.
5. Overrides bypassing zones/GPS, clearing on restart, persistent explicit
   `no_tx` and fault latches, and refusal to clear without successful checks.
6. OpenHop mode mapping, live readback shape, success=false within HTTP 200,
   uncertain write outcomes, auth failures, external control conflict, and
   suppression of unchanged writes.
7. MeshMapper types, experimental thresholds (off by default, ignored when
   disabled; SNR, effective, null fields; enabled with none set), radio-unit
   conversion, 14-day eligibility, missing square
   versus unknown scope, valid offline cache, expiry, partial/oversize data,
   unsupported filters, quota/backoff across service restart, no refetch of a
   fresh snapshot after reboot, and 304 aging.
8. Atomic config/cache/latch writes, rejected stale edits, persistence failures,
   redacted status, authenticated controls, CSRF checks, and disabled web
   interface or maps.
9. Storage writes: a steady-state run (moving fix, web polling, unchanged
   MeshMapper refresh, disk tracing disabled) produces no writes outside the
   runtime directory.
10. Minimal install: with Shapely and web packages absent, circle zones work,
    polygon zones fail config check clearly, and MeshMapper/web/map report
    unavailable.
11. Companion TCP, serial, and BLE adapters using mocked library events;
    severity filtering, prolonged-outage detection, rate limits, bounded
    reports, and reconnect behavior.
12. Channel queries, refusal of channel control, administrator-key matching,
    ambiguous identities, unrestricted-policy warnings, stale commands,
    duplicate commands, and identical web/DM controller behavior.
13. Failure announcement submission limits and deadline, no recipient-ACK wait,
    explicit Disable all TX cancellation, persistence failure, immediate
    startup `no_tx`, receive-only DM control while blocked, no outgoing
    messages before successful clear, failed-clear handling, continued
    `no_tx` after clear when required by policy, and reporting through an
    independent companion.
14. Companion public-key/contact-link display, disabled-module imports,
    transport permissions, and secret redaction.
15. Maintenance requests from the web and DMs: authorization, duplicate
    requests, disabled or denied reboot, service-only
    restart, safe mode persistence, and no TX on a blocked shared radio.
    Mock the OS reboot interface in CI.
16. Diagnostic tracing: disabled behavior, sampling and immediate events,
    confirmed versus requested modes, GPS gaps, session boundaries, bounded
    records/context, revision changes, RAM loss, explicit disk opt-in,
    rotation by total size and age, restart pruning, write failures and queue
    overflow without control interruption, authenticated exports, secret
    exclusion, and offline GeoJSON conversion without network access.
17. Watchdog notifications only after completed controller evaluations,
    restart after a simulated stall, and startup Monitor policy.
18. Untrusted time: persisted coverage unused, companion control rejected,
    status and zone policy unaffected, and trace UTC recorded as unknown.

CI must not contact a live repeater or consume MeshMapper API quota.

### Resource estimate: Raspberry Pi Zero 2W

These estimates precede implementation. Service figures derive from x86-64
import measurements with Python 3.14:

- Shapely and NumPy add about 21 MiB RSS; a 22,500-square coverage index
  adds about 11 MiB.
- `asyncio` adds about 10 MiB, and `meshcore` with its dependencies adds
  about 9 MiB.

All figures, including the OpenHop estimate, require verification on
Raspberry Pi hardware. The Pi Zero 2W has 512 MiB of RAM.

| Component | Estimated RAM | Default |
| --- | --- | --- |
| GPU reservation | 64 MiB (16 MiB with `gpu_mem=16`) | Pi OS default |
| Kernel | ~30 MiB | Required |
| Pi OS Lite base (systemd, journald, networking, SSH) | 50–80 MiB | Required |
| RAM filesystems: `/run` state and volatile journal | 2–16 MiB | Required |
| gpsd | 2–5 MiB | Required |
| OpenHop repeater with its web interface | 60–120 MiB (unverified) | Required |
| Service core with circle zones | 25–30 MiB | Required |
| Shapely and MeshMapper index\* | 25–35 MiB | Enabled |
| Web interface and map serving\* | 25–30 MiB | Enabled |
| Companion module, TCP or serial\* | 15–25 MiB | Disabled |
| Diagnostic tracing, RAM mode\* | 12–20 MiB | Disabled |
| **Default configuration** | **283–410 MiB** | |
| **All modules enabled** | **310–455 MiB** | |

\* Optional; may need to be disabled on devices with limited resources.

RAM filesystems and page cache:

- tmpfs pages count as used memory only when they contain data. The service
  keeps less than 1 MiB in `/run`: its lock, MeshMapper request accounting,
  and backoff state. Systems using a volatile journal or log2ram should cap
  in-memory logs, for example with journald `RuntimeMaxUse=16M`.
- The trace buffer and coverage dataset are process memory, not tmpfs files,
  and are included in the service rows above.
- The service does not use `/tmp`. Atomic writes use temporary files in their
  destination directories.
- Page cache is reclaimable but required for performance. Reserve 40–80 MiB
  for cached Python modules, shared libraries, OpenHop files, and the
  coverage snapshot. Without this reserve, the system repeatedly rereads files
  from storage, increasing latency and SD-card reads.
- Disk-mode tracing writes through the page cache. Dirty pages are reclaimable
  after writeback and do not add to the trace process memory.
- Swap on SD storage increases storage wear. If swap is required, prefer zram.

With the default 64 MiB GPU reservation, 102–229 MiB remains for page cache
and headroom in the default configuration, and 57–202 MiB with all modules
enabled. At the upper estimates, the all-modules configuration does not meet
the page-cache reserve. Setting `gpu_mem=16` returns 48 MiB, leaving
105–250 MiB with all modules enabled.

The target configuration runs gpsd, OpenHop, and the service with its default
modules on one Pi Zero 2W. Zone checks at 1 Hz GPS input use little CPU;
short peaks occur at startup, during MeshMapper refreshes, trace export, and
web requests. Map rendering runs in the browser. BLE companions also require
the BlueZ service, estimated at 3–6 MiB, in addition to the companion row.

Measure service and OpenHop RSS, available memory (`MemAvailable`), tmpfs
use, page-cache pressure, CPU use, refresh and export peaks, and persistent
writes on hardware. Include companion messaging and failure announcement bursts in
whole-system resource and airtime tests. If memory is limited, set
`gpu_mem=16`, then disable tracing, the companion module, the web interface,
or MeshMapper.

Validation targets on a Pi Zero 2W:

- Service steady-state RSS below 128 MiB in the default configuration and
  below 160 MiB with all modules enabled, using a representative bounded
  coverage cache.
- `MemAvailable` above 64 MiB during steady-state operation of the complete
  system.
- Average CPU use below 2% of one core over five minutes at 1 Hz GPS input,
  with no browser connected.
- Mobile layout at 360 px and desktop layout at 1280 px.
- No steady-state writes to persistent storage during extended offline
  operation with disk tracing disabled; measure with `/proc/<pid>/io`
  `write_bytes`.

## 13. Implementation sequence

1. Implement and test configuration, zones, mode policy, hysteresis, and safe
   mode without radio access.
2. Add gpsd and OpenHop clients; test against the documented API contract.
3. Add MeshMapper region discovery, filters, cache, and rate limiting.
4. Add authenticated web controls and configuration editing, then map editing
   and optional diagnostic tracing, overlays, and offline export.
5. Add optional companion reporting, authorized commands, and bounded
   failure announcement handling.
6. Add Debian packaging, systemd installation, supporting-service guidance,
   documentation, and architecture tests.

## 14. Implementation notes

Values and behaviors fixed during implementation (version 0.1.0). These
refine the sections above; hardware validation (section 12) is still pending.

Control loop:

- The controller evaluates once per second and polls OpenHop every 5 seconds,
  reading the mode again before every write.
- An external mode change is detected only while the OpenHop connection is
  continuous; after a reconnect, the observed mode is accepted as the new
  baseline. A detected conflict latches safe mode and requests `no_tx` on the
  next evaluation.
- Three consecutive OpenHop authentication or protocol errors are a fault.
  Timeouts and connection errors are reported as unavailable without latching
  safe mode.
- `policy.outside = "last"` holds the last confirmed decision, never a
  requested mode.
- Web and companion commands return after the controller's immediate
  evaluation, so the reply reflects the resulting status.

Companion:

- Failure announcement: at most 3 submissions in total, round-robin across report
  channels, with text clipped to 140 characters.
- After `clear`, the confirmation reply waits up to 15 seconds for `no_tx`
  to be released.
- Message deduplication uses the sender prefix, sender timestamp, and text.
  Messages timestamped more than 30 seconds before service start are
  rejected.

Process and packaging:

- Exit status 75 requests a restart (standalone mode re-executes itself);
  78 is an invalid configuration at startup.
- The unit omits `MemoryDenyWriteExecute=`, which can break ctypes and libffi
  callbacks. Serial companions need a drop-in that disables
  `PrivateDevices=` and adds the `dialout` group.
- The Debian package depends on `procps` for the unit's `ExecReload`
  (`kill -HUP`), and enables and starts it subject to system policy. Upgrades
  preserve enablement state and restart the service only if it is enabled.
- The project is packaged as a Debian native package (`3.0 (native)`).
