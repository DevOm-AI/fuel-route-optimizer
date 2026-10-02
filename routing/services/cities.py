import csv
import re
from functools import lru_cache
from pathlib import Path

from django.conf import settings

DEFAULT_CITIES_CSV = Path(settings.BASE_DIR) / 'data' / 'uscities.csv'


def normalize_place(name):
    """Normalize a city name for joining: lowercase, trim, 'st.' -> 'saint'."""
    name = re.sub(r'\s+', ' ', name.strip().lower())
    return re.sub(r'\bst\b\.?', 'saint', name)


def _read_cities(path):
    """One pass over the cities CSV.

    Returns ((normalized city, state) -> (lat, lng), normalized state name/code -> state code).
    The first (most populous) row wins for duplicate city names.
    """
    coords, codes = {}, {}
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            code = row['state_id'].strip().upper()
            coords.setdefault((normalize_place(row['city']), code), (float(row['lat']), float(row['lng'])))
            if code.lower() not in codes:
                codes[code.lower()] = code
                if row.get('state_name'):
                    codes[normalize_place(row['state_name'])] = code
    return coords, codes


def load_city_coords(path):
    """Map (normalized city, state) to (lat, lng)."""
    return _read_cities(path)[0]


@lru_cache(maxsize=1)
def _city_data():
    return _read_cities(DEFAULT_CITIES_CSV)


def city_index():
    """(normalized city, state) -> (lat, lng), loaded once per process."""
    return _city_data()[0]


def state_codes():
    """Normalized state name or code -> two-letter state code."""
    return _city_data()[1]


def lookup_city(text):
    """Resolve "City, ST" (or "City, State") to (lat, lng) from the local file, or None."""
    city, sep, state = text.rpartition(',')
    if not sep or not city.strip():
        return None
    code = state_codes().get(normalize_place(state))
    if code is None:
        return None
    return city_index().get((normalize_place(city), code))
