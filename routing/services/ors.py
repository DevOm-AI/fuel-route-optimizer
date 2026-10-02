"""OpenRouteService client: geocoding (local first) and directions."""

from dataclasses import dataclass

import requests
from django.conf import settings

from routing.services.cities import lookup_city

ORS_BASE_URL = 'https://api.openrouteservice.org'
TIMEOUT_SECONDS = 15
METERS_PER_MILE = 1609.344

# Continental US, Alaska and Hawaii as (min_lng, min_lat, max_lng, max_lat).
US_BOUNDS = (
    (-125.0, 24.4, -66.9, 49.4),
    (-179.2, 51.2, -129.9, 71.4),
    (-160.3, 18.9, -154.8, 22.3),
)

_session = requests.Session()


class LocationError(Exception):
    """The input location is invalid, unknown, or outside the USA (client error)."""


class ORSError(Exception):
    """OpenRouteService failed or returned an unusable response (upstream error)."""


@dataclass(frozen=True)
class Location:
    query: str
    lat: float
    lng: float
    source: str  # 'local' or 'ors'


@dataclass(frozen=True)
class Route:
    coordinates: list  # [[lng, lat], ...] along the road
    distance_miles: float


def is_in_usa(lat, lng):
    return any(
        min_lng <= lng <= max_lng and min_lat <= lat <= max_lat
        for min_lng, min_lat, max_lng, max_lat in US_BOUNDS
    )


def _request(method, path, **kwargs):
    if not settings.ORS_API_KEY:
        raise ORSError('ORS_API_KEY is not configured.')
    try:
        response = _session.request(
            method,
            f'{ORS_BASE_URL}{path}',
            headers={'Authorization': settings.ORS_API_KEY},
            timeout=TIMEOUT_SECONDS,
            **kwargs,
        )
        response.raise_for_status()
        return response.json()
    except requests.Timeout as exc:
        raise ORSError('OpenRouteService timed out.') from exc
    except requests.HTTPError as exc:
        raise ORSError(f'OpenRouteService returned HTTP {exc.response.status_code}.') from exc
    except (requests.RequestException, ValueError) as exc:
        raise ORSError('OpenRouteService request failed.') from exc


def geocode(text):
    """Resolve a US place like "Chicago, IL": local cities file first, then ORS."""
    text = (text or '').strip()
    if not text:
        raise LocationError('Location must be a non-empty string.')

    coords = lookup_city(text)
    if coords is not None:
        return Location(text, coords[0], coords[1], 'local')

    data = _request(
        'GET',
        '/geocode/search',
        params={'text': text, 'boundary.country': 'US', 'size': 1},
    )
    features = data.get('features') or []
    if not features:
        raise LocationError(f'Could not find a US location for "{text}".')
    try:
        lng, lat = features[0]['geometry']['coordinates'][:2]
        lat, lng = float(lat), float(lng)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ORSError('OpenRouteService returned an invalid geocode response.') from exc

    country = features[0].get('properties', {}).get('country_a')
    if (country and country != 'USA') or not is_in_usa(lat, lng):
        raise LocationError(f'"{text}" is outside the USA.')
    return Location(text, lat, lng, 'ors')


def directions(start, finish):
    """One ORS driving call from start to finish (Locations); returns the road line and distance."""
    data = _request(
        'POST',
        '/v2/directions/driving-car/geojson',
        # ORS expects [lng, lat] order.
        json={'coordinates': [[start.lng, start.lat], [finish.lng, finish.lat]]},
    )
    try:
        feature = data['features'][0]
        coordinates = [[float(lng), float(lat)] for lng, lat, *_ in feature['geometry']['coordinates']]
        distance_meters = float(feature['properties']['summary']['distance'])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ORSError('OpenRouteService returned an invalid directions response.') from exc
    if len(coordinates) < 2:
        raise ORSError('OpenRouteService returned an empty route.')
    return Route(coordinates, distance_meters / METERS_PER_MILE)
