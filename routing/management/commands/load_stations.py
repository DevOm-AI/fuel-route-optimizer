import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from routing.models import FuelStation
from routing.services.cities import DEFAULT_CITIES_CSV, load_city_coords, normalize_place

CANADIAN_PROVINCES = frozenset({'AB', 'BC', 'MB', 'NB', 'NS', 'ON', 'QC', 'SK', 'YT'})

DEFAULT_FUEL_CSV = Path(settings.BASE_DIR) / 'data' / 'fuel-prices-for-be-assessment.csv'


def read_fuel_rows(path):
    """Read the fuel price CSV into stripped station dicts."""
    with open(path, newline='', encoding='utf-8') as f:
        for line_no, row in enumerate(csv.DictReader(f), start=2):
            try:
                yield {
                    'opis_id': int(row['OPIS Truckstop ID']),
                    'name': row['Truckstop Name'].strip(),
                    'address': row['Address'].strip(),
                    'city': row['City'].strip(),
                    'state': row['State'].strip().upper(),
                    'price': Decimal(row['Retail Price'].strip()),
                }
            except (KeyError, ValueError, InvalidOperation) as exc:
                raise CommandError(f'{path}:{line_no}: invalid row ({exc})') from exc


def clean_stations(rows):
    """Drop Canadian rows and keep the lowest-priced row per OPIS ID."""
    cheapest = {}
    for row in rows:
        if row['state'] in CANADIAN_PROVINCES:
            continue
        current = cheapest.get(row['opis_id'])
        if current is None or row['price'] < current['price']:
            cheapest[row['opis_id']] = row
    return list(cheapest.values())


def match_stations(stations, city_coords):
    """Split stations into (matched with lat/lng, unmatched) by (city, state)."""
    matched, unmatched = [], []
    for station in stations:
        coords = city_coords.get((normalize_place(station['city']), station['state']))
        if coords is None:
            unmatched.append(station)
        else:
            matched.append({**station, 'lat': coords[0], 'lng': coords[1]})
    return matched, unmatched


class Command(BaseCommand):
    help = 'Load fuel stations from the fuel price CSV.'

    def add_arguments(self, parser):
        parser.add_argument('--fuel-csv', type=Path, default=DEFAULT_FUEL_CSV)
        parser.add_argument('--cities-csv', type=Path, default=DEFAULT_CITIES_CSV)

    def handle(self, *args, **options):
        fuel_csv, cities_csv = options['fuel_csv'], options['cities_csv']
        for path in (fuel_csv, cities_csv):
            if not path.exists():
                raise CommandError(f'File not found: {path}')

        rows = list(read_fuel_rows(fuel_csv))
        stations = clean_stations(rows)
        self.stdout.write(f'Read {len(rows)} rows; {len(stations)} unique US stations after cleaning.')

        matched, unmatched = match_stations(stations, load_city_coords(cities_csv))
        self.stdout.write(f'Matched: {len(matched)}  Unmatched (skipped): {len(unmatched)}')

        with transaction.atomic():
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(
                (FuelStation(**station) for station in matched), batch_size=1000
            )
        self.stdout.write(self.style.SUCCESS(f'Saved {len(matched)} fuel stations.'))
