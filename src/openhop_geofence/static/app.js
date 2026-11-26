/*
 * OpenHop Geofence web interface.
 *
 * Plain JavaScript with no build step. The server only provides JSON; map
 * rendering, zone drawing, and trace segmentation run in the browser.
 * Leaflet is loaded from the server's /leaflet/ path (OS package) only when
 * maps are enabled and installed.
 */
"use strict";

const COLORS = { forward: "#1f8a3b", monitor: "#2563eb", no_tx: "#c2410c", unknown: "#6b7280" };
const POLL_MS = 5000;
const GAP_SECONDS = 60;

let session = null;        // {user, role, csrf}
let status = null;         // last /api/status payload
let pollTimer = null;
let zones = [];            // raw zone tables being edited
let zonesRevision = null;
let configRevision = null;
let map = null, layers = {};

const $ = (sel) => document.querySelector(sel);

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------

async function api(path, body) {
  const opts = { credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    if (session && session.csrf) opts.headers["X-CSRF-Token"] = session.csrf;
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opts);
  let data = {};
  try { data = await resp.json(); } catch (e) { /* non-JSON error page */ }
  if (resp.status === 401 && path !== "api/login") { showLogin(); }
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

function say(text, isError) {
  const el = $("#message");
  el.textContent = text;
  el.className = "message" + (isError ? " err" : "");
  el.hidden = !text;
}

async function act(label, fn) {
  try {
    const data = await fn();
    say(`${label}: ${data.result || "done"}`);
  } catch (e) {
    say(`${label}: ${e.message}`, true);
  }
  refresh();
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

function fmtAge(s) {
  if (s === null || s === undefined) return "—";
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
}
function fmtUtc(t) { return t ? new Date(t * 1000).toISOString().replace(".000", "") : "—"; }
function fmtBytes(n) { return n == null ? "—" : n < 1048576 ? `${(n / 1024).toFixed(0)} KiB` : `${(n / 1048576).toFixed(1)} MiB`; }
function val(v) { return v === null || v === undefined || v === "" ? "—" : String(v); }

function fill(sel, rows) {
  const dl = $(sel);
  dl.replaceChildren();
  for (const [k, v] of rows) {
    if (v === undefined) continue;
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd");
    if (v instanceof Node) dd.append(v); else dd.textContent = val(v);
    dl.append(dt, dd);
  }
}

// ---------------------------------------------------------------------------
// Session
// ---------------------------------------------------------------------------

function showLogin() {
  session = null;
  clearInterval(pollTimer); pollTimer = null;
  $("#login-view").hidden = false;
  $("#initial-setup-view").hidden = true;
  $("#setup-password-view").hidden = true;
  $("#app-view").hidden = true;
  $("#who").hidden = true;
}

function showApp() {
  $("#login-view").hidden = true;
  $("#initial-setup-view").hidden = true;
  $("#setup-password-view").hidden = !session.change_password_required;
  if (session.change_password_required) {
    $("#app-view").hidden = true;
    $("#who").hidden = true;
    return;
  }
  $("#app-view").hidden = false;
  $("#who").hidden = false;
  $("#who-name").textContent = session.role === "admin" ? "Administrator" : "Operator";
  $("#admin-card").hidden = session.role !== "admin";
  startPolling();
  loadZones();
  loadConfig();
}

$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  $("#login-error").textContent = "";
  try {
    session = await api("api/login", { password: f.password.value, remember: f.remember.checked });
    f.password.value = "";
    showApp();
  } catch (e) {
    $("#login-error").textContent = e.message;
  }
});

$("#logout").addEventListener("click", async () => {
  try { await api("api/logout", {}); } catch (e) { /* ignore */ }
  showLogin();
});

$("#initial-setup-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.target, error = form.querySelector(".error");
  error.textContent = "";
  if (form.new_password.value !== form.confirm_password.value) {
    error.textContent = "Passwords do not match.";
    return;
  }
  try {
    await api("api/setup", { password: form.new_password.value });
    form.reset();
    showLogin();
    $("#login-notice").textContent = "Administrator created. Log in.";
    $("#login-notice").hidden = false;
  } catch (e) {
    error.textContent = e.message;
  }
});

document.querySelectorAll(".password-form").forEach((form) => {
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const error = form.querySelector(".error");
    error.textContent = "";
    if (form.new_password.value !== form.confirm_password.value) {
      error.textContent = "New passwords do not match.";
      return;
    }
    try {
      await api("api/password", { current_password: form.current_password.value, new_password: form.new_password.value });
      form.reset();
      showLogin();
      $("#login-notice").textContent = "Password changed. Log in again.";
      $("#login-notice").hidden = false;
    } catch (e) {
      error.textContent = e.message;
    }
  });
});

$("#secret-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.target;
  try {
    const data = await api("api/secrets", { kind: form.kind.value, value: form.value.value });
    form.value.value = "";
    say(data.result);
    refresh();
  } catch (e) {
    say(`Save credential: ${e.message}`, true);
  }
});

// ---------------------------------------------------------------------------
// Status polling (paused while the page is hidden)
// ---------------------------------------------------------------------------

function startPolling() {
  clearInterval(pollTimer);
  refresh();
  pollTimer = setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden && session && session.user) refresh(); });

async function refresh() {
  if (!session || !session.user) return;
  try {
    status = await api("api/status");
  } catch (e) {
    $("#banner").textContent = `Service unreachable: ${e.message}`;
    $("#banner").hidden = false;
    return;
  }
  render(status);
}

function render(s) {
  const c = s.control || {}, oh = s.openhop || {}, sm = s.safe_mode || {}, g = s.gps || {};
  const txLockout = s.tx_lockout || {};
  const mode = oh.observed_mode || "unknown";

  // Banner for conditions that need attention.
  const alerts = [];
  if (sm.active) alerts.push(`Safe mode active: ${sm.cause}`);
  if (sm.persistence_error) alerts.push(`Safe mode latch not persisted: ${sm.persistence_error}`);
  if (txLockout.active) alerts.push(`Manual TX lockout active: ${txLockout.cause}`);
  if (txLockout.persistence_error) alerts.push(`TX lockout not persisted: ${txLockout.persistence_error}`);
  if (s.fault) alerts.push(`Fault: ${s.fault}`);
  if (s.config && s.config.error) alerts.push(`Configuration error: ${s.config.error}`);
  if (s.maintenance && s.maintenance.pending) alerts.push(`Maintenance: ${s.maintenance.pending}`);
  $("#banner").textContent = alerts.join(" · ");
  $("#banner").hidden = alerts.length === 0;

  const badge = $("#mode-badge");
  badge.textContent = { forward: "REPEAT", monitor: "MONITOR", no_tx: "NO TX" }[mode] || "UNKNOWN";
  badge.className = `badge ${mode}`;
  $("#mode-detail").textContent = `${c.state || "—"}${c.reason ? " — " + c.reason : ""}`;
  $("#clear-safe-mode").hidden = !sm.active;
  $("#clear-tx-lockout").hidden = !txLockout.active;
  fill("#mode-list", [
    ["Control", c.state],
    ["Source", c.source],
    ["Policy result", c.policy_result],
    ["Desired mode", c.desired_mode],
    ["Repeat in", c.repeat_in_seconds != null ? `${c.repeat_in_seconds} s (hysteresis)` : undefined],
    ["Override", c.override],
    ["Outside zones", c.outside_policy],
    ["Monitor applied as", c.monitor_mode],
    ["TX permitted", s.tx_permitted ? "yes" : "no"],
  ]);

  fill("#openhop-list", [
    ["Connected", oh.connected ? "yes" : "no"],
    ["Confirmed mode", oh.observed_mode],
    ["Last known", oh.last_known_mode],
    ["Observation age", fmtAge(oh.observed_age_seconds)],
    ["Pending request", oh.request_mode],
    ["Request error", oh.request_error],
    ["Error", oh.error],
  ]);

  const z = s.zones || {};
  fill("#gps-list", [
    ["gpsd", g.connected ? "connected" : "disconnected"],
    ["Fix", g.fix ? `${g.fix_mode === 3 ? "3D" : "2D"}, ${fmtAge(g.age_seconds)} old` : "none"],
    ["Position", g.fix ? `${g.latitude}, ${g.longitude}` : "—"],
    ["Monitor zones", (z.monitor || []).join(", ") || "—"],
    ["Repeat zones", (z.repeat || []).join(", ") || "—"],
    ["Configured zones", z.count],
    ["Error", g.error],
    ["Time trusted", s.time_trusted ? "yes" : "no"],
  ]);

  const mm = s.meshmapper;
  if (!mm || !mm.enabled) {
    fill("#mm-list", [["Status", "disabled"]]);
  } else {
    const r = mm.result || {};
    fill("#mm-list", [
      ["Available", mm.available ? "yes" : `no (${mm.reason})`],
      ["Here", r.classification ? `${r.classification}${r.grid_id ? " · " + r.grid_id : ""}` : r.reason],
      ["Squares cached", mm.squares],
      ["Data generated", fmtUtc(mm.generated_at)],
      ["Last fetch", fmtUtc(mm.fetched_at)],
      ["Requests today", `${mm.requests_today} / ${mm.daily_budget}`],
      ["Backoff until", mm.backoff_until_utc ? fmtUtc(mm.backoff_until_utc) : undefined],
      ["Last error", mm.last_error],
      ["Experimental", mm.experimental ? "weak-coverage thresholds enabled" : undefined],
    ]);
  }

  const cp = s.companion || {};
  if (!cp.enabled) {
    fill("#comp-list", [["Status", "disabled"]]);
  } else {
    let link;
    if (cp.contact_uri) {
      link = document.createElement("a");
      link.href = cp.contact_uri; link.textContent = "Add contact";
    }
    fill("#comp-list", [
      ["Connected", cp.connected ? `yes (${cp.transport})` : `no (${cp.transport})`],
      ["Node", cp.node_name],
      ["Public key", cp.public_key],
      ["Contact", link],
      ["Reports", `level ${cp.report_level}, channels ${(cp.report_channels || []).join(", ")}`],
      ["Commands", `${cp.authorization}${cp.authorization === "admin_keys" ? ` (${cp.admin_keys} keys)` : ""}`],
      ["Shares radio", cp.shares_openhop_radio ? `yes, TX ${cp.tx_allowed ? "allowed" : "held"}` : "no"],
      ["Last send", cp.last_send],
      ["Suppressed", cp.suppressed_messages || undefined],
      ["Identity", cp.identity_warning],
      ["Error", cp.error],
      ["Failure announcement", sm.announce_failure_seconds != null ? `${sm.announce_failure_seconds} s until no_tx` : undefined],
    ]);
  }

  const t = s.trace || {};
  $("#trace-card").hidden = !t.enabled;
  if (t.enabled) {
    fill("#trace-list", [
      ["Recording", t.recording ? "yes" : "no"],
      ["Storage", t.storage],
      ["Records", `${t.records} (${fmtBytes(t.buffer_bytes)} / ${fmtBytes(t.max_buffer_bytes)})`],
      ["Context", `${fmtBytes(t.context_bytes)} / ${fmtBytes(t.max_context_bytes)}`],
      ["Disk limit", t.max_disk_bytes ? `${fmtBytes(t.max_disk_bytes)}, ${fmtAge(t.max_age_seconds)}` : undefined],
      ["Dropped", `${t.dropped_records} records, ${t.dropped_context} context`],
      ["Error", t.error],
    ]);
  }

  const cf = s.config || {};
  fill("#config-list", [
    ["Revision", cf.revision],
    ["Restart required", (cf.restart_required || []).join(", ") || undefined],
    ["Warnings", (cf.warnings || []).join("; ") || undefined],
    ["Version", s.version],
    ["Uptime", fmtAge(s.uptime_seconds)],
  ]);
  $("#config-details").hidden = !(s.web && s.web.config_edit);
  $("#zones-save").disabled = !(s.web && s.web.config_edit);
  $("#reboot").disabled = !(s.maintenance && s.maintenance.allow_reboot);

  if (s.web && s.web.map_enabled) initMap();
  updateMap(s);
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

document.querySelectorAll("button[data-action]").forEach((b) => {
  b.addEventListener("click", () => {
    const name = b.dataset.action;
    if (name === "disable_tx" && !confirm("Disable all TX and keep the operator TX lockout across restarts?")) return;
    act(b.textContent, () => api("api/action", { action: name }));
  });
});

$("#clear-tx-lockout").addEventListener("click", () => {
  if (!confirm("Clear the operator TX lockout? Automatic policy may enable Repeat.")) return;
  act("Clear TX lockout", () => api("api/action", { action: "clear_tx_lockout" }));
});

$("#restart").addEventListener("click", () => {
  if (!confirm("Restart the service? Mode control pauses briefly.")) return;
  act("Restart", () => api("api/maintenance", { action: "restart" }));
});

$("#reboot").addEventListener("click", () => {
  act("Reboot", () => api("api/maintenance", { action: "reboot" }));
});

// ---------------------------------------------------------------------------
// Configuration editor
// ---------------------------------------------------------------------------

async function loadConfig() {
  try {
    const d = await api("api/config");
    $("#config-text").value = d.text;
    configRevision = d.revision;
  } catch (e) { say(`Load configuration: ${e.message}`, true); }
}
$("#config-load").addEventListener("click", loadConfig);
$("#config-save").addEventListener("click", () => act("Save configuration", async () => {
  const d = await api("api/config", { text: $("#config-text").value, revision: configRevision });
  await loadConfig(); await loadZones();
  return d;
}));

// ---------------------------------------------------------------------------
// Zones
// ---------------------------------------------------------------------------

async function loadZones() {
  try {
    const d = await api("api/zones");
    zones = d.zones; zonesRevision = d.revision;
    renderZones();
  } catch (e) { say(`Load zones: ${e.message}`, true); }
}

function renderZones() {
  const tbody = $("#zone-table tbody");
  tbody.replaceChildren();
  zones.forEach((z, i) => {
    const tr = document.createElement("tr");
    for (const v of [z.id, z.name || z.id, z.role, z.shape || "circle"]) {
      const td = document.createElement("td"); td.textContent = v; tr.append(td);
    }
    const td = document.createElement("td");
    const edit = document.createElement("button"); edit.textContent = "Edit";
    edit.addEventListener("click", () => editZone(i));
    const del = document.createElement("button"); del.textContent = "Remove";
    del.addEventListener("click", () => { zones.splice(i, 1); renderZones(); say("Zone removed; save to apply."); });
    td.append(edit, " ", del); tr.append(td); tbody.append(tr);
  });
  drawZones();
}

function shapeFields() {
  const poly = $("#zone-form").shape.value === "geojson";
  document.querySelectorAll("#zone-form .circle").forEach((e) => { e.hidden = poly; });
  document.querySelectorAll("#zone-form .polygon").forEach((e) => { e.hidden = !poly; });
}
$("#zone-form").shape.addEventListener("change", shapeFields);

function editZone(i) {
  const f = $("#zone-form"), z = zones[i];
  f.index.value = i;
  f.id.value = z.id; f.name.value = z.name || ""; f.role.value = z.role; f.shape.value = z.shape || "circle";
  f.latitude.value = z.latitude ?? ""; f.longitude.value = z.longitude ?? ""; f.radius_m.value = z.radius_m ?? "";
  f.geometry.value = z.geometry ? JSON.stringify(z.geometry) : "";
  shapeFields();
  f.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

$("#zone-reset").addEventListener("click", () => { $("#zone-form").reset(); $("#zone-form").index.value = ""; shapeFields(); });

$("#zone-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const z = { id: f.id.value.trim(), role: f.role.value, shape: f.shape.value };
  if (f.name.value.trim()) z.name = f.name.value.trim();
  if (z.shape === "circle") {
    z.latitude = Number(f.latitude.value); z.longitude = Number(f.longitude.value); z.radius_m = Number(f.radius_m.value);
  } else {
    try { z.geometry = JSON.parse(f.geometry.value); } catch (e) { say("Geometry is not valid JSON", true); return; }
  }
  const i = f.index.value === "" ? -1 : Number(f.index.value);
  if (zones.some((o, j) => o.id === z.id && j !== i)) { say(`Zone id ${z.id} already exists`, true); return; }
  if (i >= 0) zones[i] = z; else zones.push(z);
  f.index.value = i >= 0 ? i : zones.length - 1;
  renderZones();
  say("Zone list updated; save to validate and apply.");
});

$("#zones-save").addEventListener("click", () => act("Save zones", async () => {
  const d = await api("api/zones", { zones, revision: zonesRevision });
  await loadZones(); await loadConfig();
  return d;
}));

// ---------------------------------------------------------------------------
// Map (optional; Leaflet from the OS package)
// ---------------------------------------------------------------------------

let mapState = "idle";  // idle | loading | ready | unavailable

async function initMap() {
  if (mapState !== "idle") return;
  mapState = "loading";
  $("#map-card").hidden = false;
  let info;
  try { info = await api("api/map"); } catch (e) { mapState = "unavailable"; return; }
  if (!info.leaflet) {
    mapState = "unavailable";
    $("#map").hidden = true;
    $("#map-notice").textContent = `Leaflet not found in ${info.leaflet_dir}. Install it with "sudo apt install libjs-leaflet" ` +
      "or set web.leaflet_dir. Zone forms and status still work.";
    $("#map-notice").hidden = false;
    return;
  }
  const css = document.createElement("link");
  css.rel = "stylesheet"; css.href = "leaflet/leaflet.css";
  document.head.append(css);
  const js = document.createElement("script");
  js.src = "leaflet/leaflet.js";
  js.onload = () => {
    map = L.map("map").setView([0, 0], 2);
    const tiles = L.tileLayer(info.tile_url, { maxZoom: 19, attribution: info.attribution }).addTo(map);
    tiles.on("tileerror", () => {
      $("#map-notice").textContent = "Map tiles are unavailable (offline?). Zones, position, and traces still display.";
      $("#map-notice").hidden = false;
    });
    layers.coverage = L.layerGroup().addTo(map);
    layers.zones = L.layerGroup().addTo(map);
    layers.trace = L.layerGroup();
    layers.position = L.layerGroup().addTo(map);
    map.on("click", (e) => {
      const f = $("#zone-form");
      if (f.shape.value !== "circle") return;
      f.latitude.value = e.latlng.lat.toFixed(6);
      f.longitude.value = e.latlng.lng.toFixed(6);
      if (!f.radius_m.value) f.radius_m.value = 500;
    });
    mapState = "ready";
    drawZones();
    if (status) updateMap(status, true);
  };
  js.onerror = () => { mapState = "unavailable"; };
  document.head.append(js);
}

function drawZones() {
  if (mapState !== "ready") return;
  layers.zones.clearLayers();
  const bounds = [];
  for (const z of zones) {
    const color = z.role === "monitor" ? COLORS.monitor : COLORS.forward;
    const label = `${z.name || z.id} (${z.role})`;
    let layer;
    if ((z.shape || "circle") === "circle" && Number.isFinite(z.latitude)) {
      layer = L.circle([z.latitude, z.longitude], { radius: z.radius_m, color, weight: 2, fillOpacity: 0.1 });
    } else if (z.geometry) {
      try { layer = L.geoJSON(z.geometry, { style: { color, weight: 2, fillOpacity: 0.1 } }); } catch (e) { continue; }
    }
    if (!layer) continue;
    layer.bindTooltip(label).addTo(layers.zones);
    try { bounds.push(layer.getBounds()); } catch (e) { /* circle before added */ }
  }
  if (bounds.length && !drawZones.fitted) {
    let b = bounds[0];
    bounds.forEach((x) => { b = b.extend(x); });
    map.fitBounds(b, { maxZoom: 15 });
    drawZones.fitted = true;
  }
}

let lastCoverageAt = 0, lastCoverageKey = "";

async function updateMap(s, first) {
  if (mapState !== "ready") return;
  const g = s.gps || {};
  layers.position.clearLayers();
  if (g.fix) {
    const mode = (s.openhop || {}).observed_mode || "unknown";
    L.circleMarker([g.latitude, g.longitude], { radius: 8, color: "#000", weight: 2, fillColor: COLORS[mode], fillOpacity: 1 })
      .bindTooltip(`Current position · ${mode}`).addTo(layers.position);
    if (first && !drawZones.fitted) map.setView([g.latitude, g.longitude], 13);
  }
  // Coverage squares near the current position, refreshed at most once a minute.
  const showCov = $("#show-coverage").checked && s.meshmapper && s.meshmapper.enabled && g.fix;
  if (!showCov) { layers.coverage.clearLayers(); lastCoverageKey = ""; return; }
  const key = `${g.latitude.toFixed(2)},${g.longitude.toFixed(2)},${s.meshmapper.generated_at}`;
  if (key === lastCoverageKey && Date.now() - lastCoverageAt < 60000) return;
  lastCoverageKey = key; lastCoverageAt = Date.now();
  try {
    const d = await api(`api/coverage?lat=${g.latitude}&lon=${g.longitude}`);
    layers.coverage.clearLayers();
    for (const sq of d.squares) {
      const [south, west, north, east] = sq.bounds;
      // Covered squares favor Monitor; uncovered/weak squares favor Repeat.
      L.rectangle([[south, west], [north, east]], {
        weight: 0, fillOpacity: 0.2, fillColor: sq.class === "covered" ? COLORS.monitor : COLORS.forward,
      }).bindTooltip(`${sq.grid_id}: ${sq.type} (${sq.class})`).addTo(layers.coverage);
    }
  } catch (e) { /* coverage overlay is best-effort */ }
}
$("#show-coverage").addEventListener("change", () => { lastCoverageKey = ""; if (status) updateMap(status); });

// ---------------------------------------------------------------------------
// Diagnostic trace view
// ---------------------------------------------------------------------------

function traceSegments(records) {
  // Same rules as openhop-geofence-trace: split at mode changes, gaps, sessions.
  const segs = [];
  let cur = null, prev = null;
  for (const r of records) {
    if (r.type !== "sample" && r.type !== "transition") continue;
    if (!r.fix || r.lat == null) { cur = null; prev = null; continue; }
    const mode = r.confirmed || "unknown";
    const split = !prev || prev.session !== r.session || (r.elapsed - prev.elapsed) > GAP_SECONDS;
    if (split) { cur = { mode, points: [] }; segs.push(cur); }
    else if (cur.mode !== mode) { cur = { mode, points: [[prev.lat, prev.lon]] }; segs.push(cur); }
    cur.points.push([r.lat, r.lon]);
    prev = r;
  }
  return segs;
}

async function drawTrace() {
  if (mapState !== "ready") return;
  layers.trace.clearLayers();
  if (!$("#show-trace").checked) { map.removeLayer(layers.trace); return; }
  try {
    const d = await api("api/trace");
    for (const item of d.context) {
      if (item.type === "square") {
        const [south, west, north, east] = item.bounds;
        L.rectangle([[south, west], [north, east]], { weight: 1, dashArray: "3", fill: false, color: COLORS.unknown })
          .bindTooltip(`historical ${item.grid_id}: ${item.coverage_type}`).addTo(layers.trace);
      }
    }
    for (const seg of traceSegments(d.records)) {
      const opts = { color: COLORS[seg.mode] || COLORS.unknown, weight: 4 };
      if (seg.mode === "unknown") opts.dashArray = "4 6";
      if (seg.points.length > 1) L.polyline(seg.points, opts).bindTooltip(seg.mode).addTo(layers.trace);
    }
    layers.trace.addTo(map);
  } catch (e) { say(`Trace: ${e.message}`, true); }
}
$("#show-trace").addEventListener("change", drawTrace);

$("#trace-start").addEventListener("click", () => act("Trace start", () => api("api/trace", { action: "start" })));
$("#trace-stop").addEventListener("click", () => act("Trace stop", () => api("api/trace", { action: "stop" })));
$("#trace-clear").addEventListener("click", () => {
  if (confirm("Clear recorded trace data?")) act("Trace clear", () => api("api/trace", { action: "clear" }));
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

(async () => {
  try {
    const d = await api("api/session");
    if (d.user) { session = d; showApp(); }
    else if (d.setup_required) {
      session = d;
      $("#initial-setup-view").hidden = false;
      $("#login-view").hidden = true;
    } else showLogin();
  } catch (e) { showLogin(); }
})();
