"""Zone geometry: circle zones (standard library) and GeoJSON polygon zones (Shapely).

Circles use the haversine formula with the mean Earth radius. The spherical
approximation has less than 0.5% distance error, which is negligible at zone
scale. Shapely is imported only when polygon zones or MeshMapper are used, so
a circle-only installation never loads Shapely or NumPy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

#: Mean Earth radius in meters (IUGG).
EARTH_RADIUS_M = 6_371_008.8

ROLE_REPEAT = "repeat"
ROLE_MONITOR = "monitor"


class ShapelyUnavailable(RuntimeError):
    """Raised when a feature needs Shapely and it is not installed."""


def load_shapely():
    """Import and return the ``shapely`` module, or raise ShapelyUnavailable."""
    try:
        import shapely  # noqa: F401
        import shapely.geometry  # noqa: F401
        import shapely.prepared  # noqa: F401
        import shapely.validation  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ShapelyUnavailable(
            "Shapely is not installed; install python3-shapely (Debian/Fedora) "
            "or the 'geo' pip extra"
        ) from exc
    return shapely


def shapely_available() -> bool:
    """Return True if Shapely can be imported."""
    try:
        load_shapely()
    except ShapelyUnavailable:
        return False
    return True


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two WGS84 points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def valid_lat_lon(lat: Any, lon: Any) -> bool:
    """Return True for finite, in-range latitude and longitude numbers."""
    if isinstance(lat, bool) or isinstance(lon, bool):
        return False
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return False
    return (
        math.isfinite(lat) and math.isfinite(lon)
        and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0
    )


@dataclass(frozen=True)
class Zone:
    """Common zone attributes. Subclasses implement :meth:`contains`."""

    id: str
    name: str
    role: str

    def contains(self, lat: float, lon: float) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def to_geojson(self) -> dict:  # pragma: no cover - abstract
        raise NotImplementedError


@dataclass(frozen=True)
class CircleZone(Zone):
    """A circle defined by a WGS84 center and a radius in meters."""

    latitude: float = 0.0
    longitude: float = 0.0
    radius_m: float = 0.0

    def contains(self, lat: float, lon: float) -> bool:
        # Points exactly on the radius are inside.
        return haversine_m(self.latitude, self.longitude, lat, lon) <= self.radius_m

    def to_geojson(self) -> dict:
        """Return a GeoJSON Point feature; ``radius_m`` is a property."""
        return {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [self.longitude, self.latitude]},
            "properties": {
                "id": self.id, "name": self.name, "role": self.role,
                "shape": "circle", "radius_m": self.radius_m,
            },
        }


@dataclass(frozen=True)
class PolygonZone(Zone):
    """A GeoJSON Polygon or MultiPolygon (``[lon, lat]`` coordinates)."""

    geometry: dict = field(default_factory=dict)
    # Prepared Shapely geometry; excluded from comparisons.
    _shape: Any = field(default=None, compare=False, repr=False)

    def contains(self, lat: float, lon: float) -> bool:
        shapely = load_shapely()
        # ``covers`` treats the outer boundary as inside and hole interiors as outside.
        return bool(self._shape.covers(shapely.geometry.Point(lon, lat)))

    def to_geojson(self) -> dict:
        return {
            "type": "Feature",
            "geometry": self.geometry,
            "properties": {"id": self.id, "name": self.name, "role": self.role, "shape": "geojson"},
        }


def build_polygon_shape(geometry: dict):
    """Validate a GeoJSON Polygon/MultiPolygon and return a prepared Shapely geometry.

    Invalid or self-intersecting geometry is rejected rather than repaired.
    Polar and antimeridian-spanning polygons are rejected because Shapely
    operates on planar longitude/latitude coordinates.
    """
    shapely = load_shapely()
    if not isinstance(geometry, dict) or geometry.get("type") not in ("Polygon", "MultiPolygon"):
        raise ValueError("geometry must be a GeoJSON Polygon or MultiPolygon")
    _check_coordinates(geometry.get("coordinates"), geometry["type"])
    try:
        shape = shapely.geometry.shape(geometry)
    except Exception as exc:  # Shapely raises several exception types for bad input
        raise ValueError(f"invalid GeoJSON geometry: {exc}") from exc
    if shape.is_empty or not shape.is_valid:
        reason = shapely.validation.explain_validity(shape)
        raise ValueError(f"invalid polygon geometry: {reason}")
    min_lon, min_lat, max_lon, max_lat = shape.bounds
    if max_lon - min_lon > 180.0:
        raise ValueError("polygons spanning the antimeridian are not supported")
    if abs(min_lat) >= 89.0 or abs(max_lat) >= 89.0:
        raise ValueError("polar polygons are not supported")
    # ``prep`` is available in Shapely 1.8 (Debian Bookworm) and 2.x.
    return shapely.prepared.prep(shape)


def _check_coordinates(coords: Any, gtype: str) -> None:
    """Check that every position is a finite, in-range ``[lon, lat]`` pair."""
    depth = 3 if gtype == "Polygon" else 4

    def walk(node: Any, level: int) -> None:
        if not isinstance(node, list) or not node:
            raise ValueError("GeoJSON coordinates must be non-empty arrays")
        if level == 1:
            if len(node) < 2 or not valid_lat_lon(node[1], node[0]):
                raise ValueError(f"invalid GeoJSON position {node!r}; expected [longitude, latitude]")
            return
        for child in node:
            walk(child, level - 1)

    walk(coords, depth)


@dataclass(frozen=True)
class ZoneMatch:
    """Zones that contain a position, split by role."""

    monitor: tuple[str, ...] = ()
    repeat: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return self.monitor + self.repeat


def match_zones(zones: list[Zone] | tuple[Zone, ...], lat: float, lon: float) -> ZoneMatch:
    """Return the names of all zones containing the point, grouped by role.

    Zone order does not affect precedence; the policy gives Monitor priority.
    """
    monitor: list[str] = []
    repeat: list[str] = []
    for zone in zones:
        if zone.contains(lat, lon):
            (monitor if zone.role == ROLE_MONITOR else repeat).append(zone.name)
    return ZoneMatch(tuple(monitor), tuple(repeat))
