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


def load_city_coords(path):
    """Map (normalized city, state) to (lat, lng); the first (most populous) row wins."""
    coords = {}
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            key = (normalize_place(row['city']), row['state_id'].strip().upper())
            coords.setdefault(key, (float(row['lat']), float(row['lng'])))
    return coords


@lru_cache(maxsize=1)
def city_index():
    """(normalized city, state) -> (lat, lng), loaded once per process."""
    return load_city_coords(DEFAULT_CITIES_CSV)


@lru_cache(maxsize=1)
def state_codes():
    """Normalized state name or code -> two-letter state code."""
    codes = {}
    with open(DEFAULT_CITIES_CSV, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            code = row['state_id'].strip().upper()
            codes.setdefault(code.lower(), code)
            codes.setdefault(normalize_place(row['state_name']), code)
    return codes


def lookup_city(text):
    """Resolve "City, ST" (or "City, State") to (lat, lng) from the local file, or None."""
    city, sep, state = text.rpartition(',')
    if not sep or not city.strip():
        return None
    code = state_codes().get(normalize_place(state))
    if code is None:
        return None
    return city_index().get((normalize_place(city), code))
