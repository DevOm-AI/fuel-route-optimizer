"""Fuel stations held in memory with a KD-tree for fast "near the route" lookups."""

from dataclasses import dataclass
from decimal import Decimal

import numpy as np
from scipy.spatial import cKDTree

from routing.models import FuelStation
from routing.services.geo import EARTH_RADIUS_MILES, haversine_miles, mile_markers

DEFAULT_RADIUS_MILES = 10.0
ROUTE_STEP_MILES = 1.0


@dataclass(frozen=True)
class Station:
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    price: Decimal
    lat: float
    lng: float


@dataclass(frozen=True)
class NearbyStation:
    station: Station
    mile_marker: float  # miles from the start of the route
    distance_miles: float  # straight-line distance from the route


def _unit_vectors(lat, lng):
    """Points on the unit sphere, so Euclidean (chord) distance tracks great-circle distance."""
    lat, lng = np.radians(lat), np.radians(lng)
    return np.column_stack((np.cos(lat) * np.cos(lng), np.cos(lat) * np.sin(lng), np.sin(lat)))


def _chord_for_miles(miles):
    return 2 * np.sin(miles / (2 * EARTH_RADIUS_MILES))


class StationIndex:
    def __init__(self, stations):
        self.stations = list(stations)
        self.lat = np.array([s.lat for s in self.stations], dtype=float)
        self.lng = np.array([s.lng for s in self.stations], dtype=float)
        self.tree = cKDTree(_unit_vectors(self.lat, self.lng)) if self.stations else None

    def near_route(self, coordinates, radius_miles=DEFAULT_RADIUS_MILES, total_miles=None):
        """Stations within radius_miles of a [lng, lat] line, one entry each, sorted by mile marker.

        When total_miles is given (e.g. the road distance from ORS), mile markers are scaled so
        the end of the line sits at total_miles.
        """
        if self.tree is None or len(coordinates) == 0:
            return []

        points = np.asarray(coordinates, dtype=float)
        markers = mile_markers(points)
        if total_miles is not None and markers[-1] > 0:
            markers = markers * (total_miles / markers[-1])
        keep = thin_route(markers)
        route_lng, route_lat, route_miles = points[keep, 0], points[keep, 1], markers[keep]

        hits = self.tree.query_ball_point(
            _unit_vectors(route_lat, route_lng), _chord_for_miles(radius_miles)
        )
        route_idx = np.repeat(np.arange(len(hits)), [len(h) for h in hits])
        station_idx = np.fromiter((j for h in hits for j in h), dtype=int, count=len(route_idx))
        if len(station_idx) == 0:
            return []

        distances = haversine_miles(
            route_lat[route_idx], route_lng[route_idx], self.lat[station_idx], self.lng[station_idx]
        )
        # For each station keep the closest route point (sort by station, then distance).
        order = np.lexsort((distances, station_idx))
        station_idx, route_idx, distances = station_idx[order], route_idx[order], distances[order]
        first = np.concatenate(([True], station_idx[1:] != station_idx[:-1]))

        nearby = [
            NearbyStation(self.stations[s], float(route_miles[r]), float(d))
            for s, r, d in zip(station_idx[first], route_idx[first], distances[first])
        ]
        nearby.sort(key=lambda n: (n.mile_marker, n.station.price, n.station.opis_id))
        return nearby


def thin_route(markers, step_miles=ROUTE_STEP_MILES):
    """Indices of roughly one route point per step_miles, always keeping the first and last."""
    markers = np.asarray(markers, dtype=float)
    if len(markers) == 0:
        return np.zeros(0, dtype=int)
    targets = np.arange(0.0, markers[-1], step_miles)
    idx = np.searchsorted(markers, targets)
    return np.unique(np.concatenate((idx, [0, len(markers) - 1])))


_index = None


def get_station_index():
    """The process-wide station index, built from the database on first use."""
    global _index
    if _index is not None:
        return _index
    index = StationIndex(
        Station(s.opis_id, s.name, s.address, s.city, s.state, s.price, s.lat, s.lng)
        for s in FuelStation.objects.all()
    )
    if index.stations:  # don't pin an empty index if load_stations hasn't run yet
        _index = index
    return index


def reset_station_index():
    global _index
    _index = None
