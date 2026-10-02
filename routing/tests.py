import tempfile
from decimal import Decimal
from io import StringIO
from pathlib import Path

from unittest import mock

import requests
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from routing.management.commands.load_stations import clean_stations, match_stations
from routing.models import FuelStation
from routing.services import ors
from routing.services.cities import lookup_city, normalize_place


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


def _response(payload=None, status=200):
    response = mock.Mock(status_code=status)
    response.json.return_value = payload
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
    return response


def _geocode_payload(lng, lat, country='USA'):
    return {'features': [{'geometry': {'coordinates': [lng, lat]}, 'properties': {'country_a': country}}]}


class LookupCityTests(SimpleTestCase):
    def test_finds_city_by_state_code_or_name(self):
        lat, lng = lookup_city('Chicago, IL')
        self.assertAlmostEqual(lat, 41.8, places=0)
        self.assertAlmostEqual(lng, -87.7, places=0)
        self.assertEqual(lookup_city('chicago, illinois'), (lat, lng))

    def test_returns_none_for_unknown_or_unparseable_input(self):
        self.assertIsNone(lookup_city('Atlantis, ZZ'))
        self.assertIsNone(lookup_city('Chicago'))
        self.assertIsNone(lookup_city(', IL'))


@override_settings(ORS_API_KEY='test-key')
class GeocodeTests(SimpleTestCase):
    def setUp(self):
        patcher = mock.patch.object(ors._session, 'request')
        self.request = patcher.start()
        self.addCleanup(patcher.stop)

    def test_local_city_needs_no_api_call(self):
        location = ors.geocode('  New York, NY ')
        self.assertEqual((location.query, location.source), ('New York, NY', 'local'))
        self.request.assert_not_called()

    def test_falls_back_to_ors_geocode(self):
        self.request.return_value = _response(_geocode_payload(-77.03, 38.89))
        location = ors.geocode('1600 Pennsylvania Ave, Washington DC')
        self.assertEqual((location.lat, location.lng, location.source), (38.89, -77.03, 'ors'))
        method, url = self.request.call_args.args
        kwargs = self.request.call_args.kwargs
        self.assertEqual((method, url), ('GET', f'{ors.ORS_BASE_URL}/geocode/search'))
        self.assertEqual(kwargs['params']['boundary.country'], 'US')
        self.assertEqual(kwargs['params']['size'], 1)
        self.assertEqual(kwargs['timeout'], ors.TIMEOUT_SECONDS)
        self.assertEqual(kwargs['headers']['Authorization'], 'test-key')

    def test_rejects_empty_input(self):
        for value in ('', '   ', None):
            with self.assertRaises(ors.LocationError):
                ors.geocode(value)
        self.request.assert_not_called()

    def test_no_result_is_a_location_error(self):
        self.request.return_value = _response({'features': []})
        with self.assertRaises(ors.LocationError):
            ors.geocode('Nowhere Special')

    def test_rejects_points_outside_usa(self):
        self.request.return_value = _response(_geocode_payload(2.35, 48.85, country='FRA'))
        with self.assertRaisesMessage(ors.LocationError, 'outside the USA'):
            ors.geocode('Paris, France')
        self.request.return_value = _response(_geocode_payload(-99.13, 19.43, country=None))
        with self.assertRaisesMessage(ors.LocationError, 'outside the USA'):
            ors.geocode('Somewhere south')

    def test_upstream_failures_raise_ors_error(self):
        for failure in (_response(status=500), requests.Timeout(), requests.ConnectionError()):
            if isinstance(failure, Exception):
                self.request.side_effect, self.request.return_value = failure, None
            else:
                self.request.side_effect, self.request.return_value = None, failure
            with self.assertRaises(ors.ORSError):
                ors.geocode('Some Unknown Place')

    @override_settings(ORS_API_KEY='')
    def test_missing_api_key_raises_ors_error(self):
        with self.assertRaisesMessage(ors.ORSError, 'ORS_API_KEY'):
            ors.geocode('Some Unknown Place')
        self.request.assert_not_called()


def _directions_payload(coordinates, distance_meters):
    return {
        'features': [
            {
                'geometry': {'coordinates': coordinates},
                'properties': {'summary': {'distance': distance_meters}},
            }
        ]
    }


@override_settings(ORS_API_KEY='test-key')
class DirectionsTests(SimpleTestCase):
    start = ors.Location('Chicago, IL', 41.88, -87.63, 'local')
    finish = ors.Location('St. Louis, MO', 38.63, -90.20, 'local')

    def setUp(self):
        patcher = mock.patch.object(ors._session, 'request')
        self.request = patcher.start()
        self.addCleanup(patcher.stop)

    def test_posts_lng_lat_pairs_and_parses_route(self):
        line = [[-87.63, 41.88], [-89.0, 40.0], [-90.20, 38.63]]
        self.request.return_value = _response(_directions_payload(line, 476_000))
        route = ors.directions(self.start, self.finish)

        method, url = self.request.call_args.args
        self.assertEqual((method, url), ('POST', f'{ors.ORS_BASE_URL}/v2/directions/driving-car/geojson'))
        self.assertEqual(
            self.request.call_args.kwargs['json'],
            {'coordinates': [[-87.63, 41.88], [-90.20, 38.63]]},
        )
        self.assertEqual(self.request.call_args.kwargs['timeout'], ors.TIMEOUT_SECONDS)
        self.assertEqual(route.coordinates, line)
        self.assertAlmostEqual(route.distance_miles, 476_000 / 1609.344)

    def test_invalid_payload_raises_ors_error(self):
        for payload in ({}, {'features': []}, _directions_payload([[-87.6, 41.8]], 10)):
            self.request.return_value = _response(payload)
            with self.assertRaises(ors.ORSError):
                ors.directions(self.start, self.finish)

    def test_http_error_raises_ors_error(self):
        self.request.return_value = _response({'error': 'quota'}, status=429)
        with self.assertRaisesMessage(ors.ORSError, 'HTTP 429'):
            ors.directions(self.start, self.finish)
