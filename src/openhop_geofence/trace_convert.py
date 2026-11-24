"""Convert a diagnostics trace export to GeoJSON for offline viewing.

Usage::

    openhop-geofence-trace export.zip -o trace.geojson
    openhop-geofence-trace records.jsonl --context context.jsonl

The output is a GeoJSON FeatureCollection that opens in QGIS, geojson.io, or
any GIS tool:

* ``LineString`` track segments, one per run of the same confirmed mode.
  Segments split at mode changes, GPS gaps, and session boundaries.
* ``Point`` features for transitions and events that have a known location.
* Zone and MeshMapper square context (``layer`` property ``zone``/``square``).

Only the Python standard library is used, so the tool runs on any desktop.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import zipfile
from collections.abc import Iterable, Iterator
from typing import Any

#: A gap longer than this (seconds of service elapsed time) splits a segment.
DEFAULT_GAP_SECONDS = 60.0


def read_jsonl(lines: Iterable[bytes | str]) -> Iterator[dict]:
    """Yield JSON objects, skipping blank and malformed lines (e.g. a torn tail)."""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def load_export(path: str, context_path: str | None = None) -> tuple[list[dict], list[dict]]:
    """Read records and context from an export ZIP or JSON Lines files."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            records = list(read_jsonl(io.TextIOWrapper(zf.open("records.jsonl"), "utf-8"))) \
                if "records.jsonl" in names else []
            context = list(read_jsonl(io.TextIOWrapper(zf.open("context.jsonl"), "utf-8"))) \
                if "context.jsonl" in names else []
        return records, context
    with open(path, "rb") as fh:
        records = list(read_jsonl(fh))
    context: list[dict] = []
    if context_path:
        with open(context_path, "rb") as fh:
            context = list(read_jsonl(fh))
    return records, context


def _mode_of(rec: dict) -> str:
    """Confirmed OpenHop mode, or ``unknown`` when no fresh observation existed."""
    return rec.get("confirmed") or "unknown"


def to_geojson(records: list[dict], context: list[dict], gap_seconds: float = DEFAULT_GAP_SECONDS) -> dict:
    """Build a GeoJSON FeatureCollection from trace records and context."""
    features: list[dict] = []

    # Context first so tracks draw above it in viewers that respect order.
    for item in context:
        if item.get("type") == "zones":
            for f in item.get("geojson", {}).get("features", []):
                f = dict(f)
                f["properties"] = dict(f.get("properties", {}), layer="zone",
                                       config_revision=item.get("config_revision"))
                features.append(f)
        elif item.get("type") == "square":
            s, w, n, e = item["bounds"]
            features.append({
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]},
                "properties": {
                    "layer": "square", "grid_id": item.get("grid_id"),
                    "coverage_type": item.get("coverage_type"), "timestamp": item.get("timestamp"),
                    "snr": item.get("snr"), "coverage_revision": item.get("coverage_revision"),
                },
            })

    segment: list[dict] = []
    last_pos: dict | None = None

    def close_segment() -> None:
        if len(segment) >= 2:
            first, last = segment[0], segment[-1]
            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [[r["lon"], r["lat"]] for r in segment]},
                "properties": {
                    "layer": "track", "mode": _mode_of(first), "session": first.get("session"),
                    "start_utc": first.get("utc"), "end_utc": last.get("utc"),
                    "start_elapsed": first.get("elapsed"), "end_elapsed": last.get("elapsed"),
                    "points": len(segment),
                },
            })
        segment.clear()

    for rec in records:
        kind = rec.get("type")
        if kind == "event":
            # Events carry no position; place them at the last known fix of the session.
            if last_pos and last_pos.get("session") == rec.get("session"):
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [last_pos["lon"], last_pos["lat"]]},
                    "properties": {"layer": "event", "level": rec.get("level"), "kind": rec.get("kind"),
                                   "message": rec.get("message"), "utc": rec.get("utc"),
                                   "position": "last known fix"},
                })
            continue
        if kind not in ("sample", "transition"):
            continue
        has_pos = rec.get("fix") and rec.get("lat") is not None and rec.get("lon") is not None
        if not has_pos:
            close_segment()
            continue
        prev = segment[-1] if segment else None
        if prev is not None:
            new_session = prev.get("session") != rec.get("session")
            gap = (rec.get("elapsed") or 0) - (prev.get("elapsed") or 0) > gap_seconds
            if new_session or gap:
                close_segment()
            elif _mode_of(prev) != _mode_of(rec):
                # Join the new segment to the boundary point so the track stays continuous.
                close_segment()
                segment.append(dict(prev, confirmed=rec.get("confirmed")))
        segment.append(rec)
        last_pos = rec
        if kind == "transition":
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [rec["lon"], rec["lat"]]},
                "properties": {
                    "layer": "transition", "utc": rec.get("utc"), "elapsed": rec.get("elapsed"),
                    "confirmed": rec.get("confirmed"), "desired": rec.get("desired"),
                    "result": rec.get("result"), "source": rec.get("source"), "reason": rec.get("reason"),
                    "zones": rec.get("zones"), "coverage": rec.get("coverage"),
                    "request_error": rec.get("request_error"),
                },
            })
    close_segment()
    return {"type": "FeatureCollection", "features": features}


def summarize(records: list[dict]) -> dict[str, Any]:
    """Return simple counts for a quick text summary."""
    counts: dict[str, int] = {}
    sessions = set()
    for r in records:
        counts[r.get("type", "?")] = counts.get(r.get("type", "?"), 0) + 1
        sessions.add(r.get("session"))
    return {"records": len(records), "sessions": len(sessions - {None}), "by_type": counts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="openhop-geofence-trace",
        description="Convert an openhop-geofence diagnostics export to GeoJSON.",
    )
    parser.add_argument("input", help="export ZIP, or records.jsonl")
    parser.add_argument("--context", help="context.jsonl when the input is a records file")
    parser.add_argument("-o", "--output", help="output GeoJSON file (default: standard output)")
    parser.add_argument("--gap-seconds", type=float, default=DEFAULT_GAP_SECONDS,
                        help=f"split tracks at GPS gaps longer than this (default {DEFAULT_GAP_SECONDS:g})")
    parser.add_argument("--summary", action="store_true", help="print record counts instead of GeoJSON")
    args = parser.parse_args(argv)
    try:
        records, context = load_export(args.input, args.context)
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        print(f"openhop-geofence-trace: {exc}", file=sys.stderr)
        return 1
    if args.summary:
        print(json.dumps(summarize(records), indent=2))
        return 0
    data = json.dumps(to_geojson(records, context, args.gap_seconds), separators=(",", ":"))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(data)
    else:
        sys.stdout.write(data + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
