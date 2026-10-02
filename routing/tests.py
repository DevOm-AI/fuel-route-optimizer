import tempfile
from decimal import Decimal
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from routing.management.commands.load_stations import clean_stations, match_stations, normalize_place
from routing.models import FuelStation


def _row(opis_id, state='TX', price='3.50'):
    return {
        'opis_id': opis_id,
        'name': f'Station {opis_id}',
        'address': 'I-10, EXIT 1',
        'city': 'Austin',
        'state': state,
        'price': Decimal(price),
    }


class CleanStationsTests(SimpleTestCase):
    def test_drops_canadian_rows(self):
        cleaned = clean_stations([_row(1, 'ON'), _row(2, 'BC'), _row(3, 'TX')])
        self.assertEqual([s['opis_id'] for s in cleaned], [3])

    def test_dedupes_keeping_lowest_price(self):
        cleaned = clean_stations([_row(1, price='3.90'), _row(1, price='3.10'), _row(1, price='3.50')])
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]['price'], Decimal('3.10'))


class NormalizePlaceTests(SimpleTestCase):
    def test_lowercases_trims_and_expands_saint(self):
        self.assertEqual(normalize_place('  St. Louis  '), 'saint louis')
        self.assertEqual(normalize_place('St Johns'), 'saint johns')
        self.assertEqual(normalize_place('Saint  Paul'), 'saint paul')

    def test_leaves_st_inside_words_alone(self):
        self.assertEqual(normalize_place('Stockton'), 'stockton')
        self.assertEqual(normalize_place('Easton'), 'easton')


class MatchStationsTests(SimpleTestCase):
    def test_splits_matched_and_unmatched(self):
        coords = {('saint louis', 'MO'): (38.6, -90.2)}
        stations = [
            {**_row(1, 'MO'), 'city': 'St. Louis'},
            {**_row(2, 'MO'), 'city': 'Nowhere'},
        ]
        matched, unmatched = match_stations(stations, coords)
        self.assertEqual([(s['opis_id'], s['lat'], s['lng']) for s in matched], [(1, 38.6, -90.2)])
        self.assertEqual([s['opis_id'] for s in unmatched], [2])


class LoadStationsCommandTests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.fuel_csv = Path(tmp.name) / 'fuel.csv'
        self.cities_csv = Path(tmp.name) / 'cities.csv'
        self.fuel_csv.write_text(
            'OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n'
            '1,ALPHA,"I-44, EXIT 283",Big Cabin,OK,307,3.20\n'
            '1,ALPHA DUP,"I-44, EXIT 283",Big Cabin,OK,307,3.10\n'
            '2,BRAVO,"I-94, EXIT 143",Tomah   ,WI,420,3.30\n'
            '3,CHARLIE,HWY 1,Toronto,ON,1,2.90\n'
            '4,DELTA,HWY 2,Nowhere,TX,1,3.00\n'
        )
        self.cities_csv.write_text(
            'city,state_id,lat,lng\n'
            'Big Cabin,OK,36.54,-95.22\n'
            'Tomah,WI,43.98,-90.50\n'
        )

    def _run(self):
        out = StringIO()
        call_command('load_stations', fuel_csv=self.fuel_csv, cities_csv=self.cities_csv, stdout=out)
        return out.getvalue()

    def test_loads_matched_stations_and_reports_counts(self):
        output = self._run()
        self.assertIn('Matched: 2  Unmatched (skipped): 1', output)
        stations = {s.opis_id: s for s in FuelStation.objects.all()}
        self.assertEqual(set(stations), {1, 2})
        self.assertEqual(stations[1].price, Decimal('3.10'))
        self.assertEqual((stations[2].city, stations[2].lat, stations[2].lng), ('Tomah', 43.98, -90.50))

    def test_rerun_replaces_existing_rows(self):
        self._run()
        self._run()
        self.assertEqual(FuelStation.objects.count(), 2)
