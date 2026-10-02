import hashlib
import json
from urllib.parse import urlencode

from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from routing.services import ors
from routing.services.cities import normalize_place
from routing.services.optimizer import NoFuelInRangeError, plan_route_fuel
from routing.services.stations import get_station_index

MAX_LOCATION_LENGTH = 200
ROUTE_CACHE_SECONDS = 60 * 60 * 24


class InvalidRequest(Exception):
    pass


def _error(message, status):
    return JsonResponse({'error': message}, status=status)


def _validate_locations(data):
    """Return stripped (start, finish) from a mapping, or raise InvalidRequest."""
    values = []
    for field in ('start', 'finish'):
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            raise InvalidRequest(f'"{field}" is required and must be a non-empty string.')
        if len(value) > MAX_LOCATION_LENGTH:
            raise InvalidRequest(f'"{field}" must be at most {MAX_LOCATION_LENGTH} characters.')
        values.append(value.strip())
    if values[0].lower() == values[1].lower():
        raise InvalidRequest('"start" and "finish" must be different locations.')
    return values


def _parse_route_request(body):
    """Return (start, finish) strings from a JSON body, or raise InvalidRequest."""
    try:
        data = json.loads(body or b'')
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidRequest('Request body must be valid JSON.') from exc
    if not isinstance(data, dict):
        raise InvalidRequest('Request body must be a JSON object.')
    return _validate_locations(data)


def build_route_result(start_text, finish_text):
    """geocode -> directions -> stations near route -> optimizer, as a JSON-ready dict."""
    start = ors.geocode(start_text)
    finish = ors.geocode(finish_text)
    route = ors.directions(start, finish)
    nearby = get_station_index().near_route(route.coordinates, total_miles=route.distance_miles)
    plan = plan_route_fuel(nearby, route.distance_miles, start)

    api_calls = 1 + sum(loc.source == 'ors' for loc in (start, finish))
    return {
        'start': {'query': start.query, 'lat': start.lat, 'lng': start.lng},
        'finish': {'query': finish.query, 'lat': finish.lat, 'lng': finish.lng},
        'total_distance_miles': round(route.distance_miles, 1),
        **plan,
        'route': {
            'type': 'Feature',
            'geometry': {'type': 'LineString', 'coordinates': route.coordinates},
            'properties': {},
        },
        'external_api_calls': api_calls,
    }


def route_cache_key(start_text, finish_text):
    normalized = f'{normalize_place(start_text)}|{normalize_place(finish_text)}'
    return 'route:' + hashlib.sha256(normalized.encode()).hexdigest()


def get_route_result(start_text, finish_text):
    """The route result for (start, finish), computed once and then served from the cache."""
    key = route_cache_key(start_text, finish_text)
    result = cache.get(key)
    if result is not None:
        return {**result, 'external_api_calls': 0}
    result = build_route_result(start_text, finish_text)
    cache.set(key, result, ROUTE_CACHE_SECONDS)
    return result


@csrf_exempt
@require_POST
def route(request):
    try:
        start_text, finish_text = _parse_route_request(request.body)
        result = get_route_result(start_text, finish_text)
    except (InvalidRequest, ors.LocationError) as exc:
        return _error(str(exc), 400)
    except NoFuelInRangeError as exc:
        return _error(str(exc), 422)
    except ors.ORSError as exc:
        return _error(str(exc), 502)

    query = urlencode({'start': start_text, 'finish': finish_text})
    result['map_url'] = request.build_absolute_uri(f"{reverse('route-map')}?{query}")
    return JsonResponse(result)


@require_GET
def route_map(request):
    """Leaflet map of a route; served from the cache, computed once if missing."""
    try:
        start_text, finish_text = _validate_locations(request.GET)
        result = get_route_result(start_text, finish_text)
    except (InvalidRequest, ors.LocationError) as exc:
        return HttpResponse(str(exc), status=400, content_type='text/plain')
    except NoFuelInRangeError as exc:
        return HttpResponse(str(exc), status=422, content_type='text/plain')
    except ors.ORSError as exc:
        return HttpResponse(str(exc), status=502, content_type='text/plain')

    map_data = {key: result[key] for key in (
        'start', 'finish', 'route', 'fuel_stops',
        'total_distance_miles', 'total_gallons', 'total_fuel_cost',
    )}
    return render(request, 'map.html', {'map_data': map_data, 'result': result})
