import json
import logging
import tempfile
from decimal import Decimal
from io import StringIO
from pathlib import Path

from unittest import mock

import numpy as np
import requests
from django.core.cache import cache
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from routing.apps import warm_caches
from routing.management.commands.load_stations import clean_stations, match_stations
from routing.models import FuelStation
from routing.services import ors
from routing.services.cities import lookup_city, normalize_place
from routing.services.geo import haversine_miles, mile_markers
from routing.services.optimizer import FuelOption, NoFuelInRangeError, plan_fuel_stops, plan_route_fuel
from routing.services.stations import NearbyStation, Station, StationIndex, get_station_index, reset_station_index, thin_route


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


class MileMarkerTests(SimpleTestCase):
    def test_haversine_known_distance(self):
        # New York -> Los Angeles is ~2,445 miles as the crow flies.
        self.assertAlmostEqual(float(haversine_miles(40.7128, -74.0060, 34.0522, -118.2437)), 2445, delta=5)

    def test_cumulative_miles_along_line(self):
        # One degree of latitude is ~69.1 miles.
        markers = mile_markers([[-90.0, 30.0], [-90.0, 31.0], [-90.0, 32.0]])
        self.assertEqual(markers[0], 0.0)
        self.assertAlmostEqual(markers[1], 69.1, delta=0.1)
        self.assertAlmostEqual(markers[2], 2 * markers[1], delta=1e-9)

    def test_empty_and_single_point_lines(self):
        self.assertEqual(len(mile_markers([])), 0)
        self.assertEqual(list(mile_markers([[-90.0, 30.0]])), [0.0])


def _station(opis_id, lat, lng, price='3.00'):
    return Station(opis_id, f'S{opis_id}', 'I-55, EXIT 1', 'Town', 'IL', Decimal(price), lat, lng)


class StationsNearRouteTests(SimpleTestCase):
    # A north-south line along lng -90 from lat 30 to 32 (~138 miles); 0.01 deg lat ~ 0.69 mi.
    line = [[-90.0, 30.0 + i * 0.01] for i in range(201)]

    def test_thin_route_keeps_about_one_point_per_mile(self):
        markers = mile_markers(self.line)
        idx = thin_route(markers)
        self.assertEqual(idx[0], 0)
        self.assertEqual(idx[-1], len(self.line) - 1)
        self.assertLessEqual(np.diff(markers[idx]).max(), 1.0 + 0.7)
        self.assertLess(len(idx), len(self.line))

    def test_filters_by_radius_and_sorts_by_mile_marker(self):
        index = StationIndex([
            _station(1, 31.5, -90.0),            # on the line, ~103.6 mi
            _station(2, 30.5, -90.1),            # ~6 mi off the line, ~34.5 mi
            _station(3, 31.0, -90.5),            # ~30 mi off the line: excluded
            _station(4, 40.0, -100.0),           # far away: excluded
        ])
        nearby = index.near_route(self.line, radius_miles=10)
        self.assertEqual([n.station.opis_id for n in nearby], [2, 1])
        self.assertAlmostEqual(nearby[0].mile_marker, 34.5, delta=1.0)
        self.assertAlmostEqual(nearby[0].distance_miles, 6.0, delta=0.2)
        self.assertAlmostEqual(nearby[1].mile_marker, 103.6, delta=1.0)
        self.assertLess(nearby[1].distance_miles, 0.5)

    def test_each_station_appears_once_at_its_nearest_point(self):
        index = StationIndex([_station(1, 31.0, -90.05)])
        nearby = index.near_route(self.line, radius_miles=10)
        self.assertEqual(len(nearby), 1)
        self.assertAlmostEqual(nearby[0].mile_marker, mile_markers([[-90, 30], [-90, 31]])[1], delta=1.0)

    def test_scales_mile_markers_to_total_miles(self):
        index = StationIndex([_station(1, 31.0, -90.0)])  # halfway along the line
        nearby = index.near_route(self.line, total_miles=200)
        self.assertAlmostEqual(nearby[0].mile_marker, 100.0, delta=1.0)

    def test_empty_index_or_route(self):
        self.assertEqual(StationIndex([]).near_route(self.line), [])
        self.assertEqual(StationIndex([_station(1, 30.0, -90.0)]).near_route([]), [])


class StationIndexLoadingTests(TestCase):
    def setUp(self):
        reset_station_index()
        self.addCleanup(reset_station_index)

    def test_empty_table_is_not_pinned(self):
        self.assertEqual(get_station_index().stations, [])
        FuelStation.objects.create(
            opis_id=1, name='A', address='X', city='Tomah', state='WI',
            price=Decimal('3.10'), lat=43.98, lng=-90.50,
        )
        self.assertEqual(len(get_station_index().stations), 1)

    def test_warm_caches_builds_index_once(self):
        FuelStation.objects.create(
            opis_id=1, name='A', address='X', city='Tomah', state='WI',
            price=Decimal('3.10'), lat=43.98, lng=-90.50,
        )
        with self.assertLogs('routing', 'INFO') as logs:
            warm_caches()
        self.assertIn('Warmed caches: 1 stations', logs.output[0])
        with self.assertNumQueries(0):
            get_station_index()

    def test_loads_once_from_database(self):
        FuelStation.objects.create(
            opis_id=1, name='A', address='X', city='Tomah', state='WI',
            price=Decimal('3.10'), lat=43.98, lng=-90.50,
        )
        index = get_station_index()
        self.assertEqual([s.opis_id for s in index.stations], [1])
        self.assertIs(get_station_index(), index)


class GreedyOptimizerTests(SimpleTestCase):
    def test_virtual_start_is_priced_like_the_first_station(self):
        purchases = plan_fuel_stops([FuelOption(50, 3.0, 'A'), FuelOption(300, 3.5, 'B')], 400)
        self.assertIsNone(purchases[0].option.data)
        self.assertEqual(purchases[0].option.price, 3.0)
        self.assertAlmostEqual(sum(p.gallons for p in purchases), 40.0)

    def test_fills_up_and_moves_to_cheapest_in_range_when_none_cheaper(self):
        # From the 3.00 start nothing is cheaper; finish is 900 mi away -> fill 500 mi,
        # go to B (cheapest in range), then buy just enough to finish.
        options = [FuelOption(0, 3.0, 'A'), FuelOption(200, 3.6, 'X'), FuelOption(450, 3.2, 'B')]
        purchases = plan_fuel_stops(options, 900)
        self.assertEqual([p.option.data for p in purchases], [None, 'B'])
        self.assertAlmostEqual(purchases[0].gallons, 50.0)
        self.assertAlmostEqual(purchases[1].gallons, (900 - 450 - 50) / 10)
        self.assertAlmostEqual(sum(p.gallons for p in purchases), 90.0)

    def test_ignores_stations_past_the_finish_and_handles_zero_distance(self):
        purchases = plan_fuel_stops([FuelOption(10, 3.0, 'A'), FuelOption(150, 1.0, 'Z')], 100)
        self.assertEqual([p.option.data for p in purchases], [None])
        self.assertEqual(plan_fuel_stops([FuelOption(0, 3.0)], 0), [])

    def test_no_stations_at_all_raises(self):
        with self.assertRaises(NoFuelInRangeError):
            plan_fuel_stops([], 100)


class FuelPlanOutputTests(SimpleTestCase):
    start = ors.Location('Chicago, IL', 41.88, -87.63, 'local')

    def test_builds_stops_and_totals(self):
        nearby = [
            NearbyStation(_station(1, 41.0, -88.0, price='3.333333'), 20.0, 1.0),
            NearbyStation(_station(2, 40.0, -89.0, price='2.999999'), 300.0, 2.0),
        ]
        plan = plan_route_fuel(nearby, 650.0, self.start)

        start_stop, station_stop = plan['fuel_stops']
        self.assertEqual(start_stop['type'], 'start')
        self.assertEqual(start_stop['priced_as'], 'S1')
        self.assertEqual((start_stop['lat'], start_stop['lng'], start_stop['mile_marker']), (41.88, -87.63, 0.0))
        self.assertEqual(start_stop['gallons'], 30.0)  # 300 mi to the cheaper station
        self.assertEqual(start_stop['price'], 3.333)

        self.assertEqual(station_stop['type'], 'station')
        self.assertEqual(
            {k: station_stop[k] for k in ('name', 'city', 'state', 'mile_marker', 'gallons')},
            {'name': 'S2', 'city': 'Town', 'state': 'IL', 'mile_marker': 300.0, 'gallons': 35.0},
        )
        self.assertEqual(plan['total_gallons'], 65.0)
        # Rounded once at the end, from unrounded stop costs.
        self.assertEqual(plan['total_fuel_cost'], round(30 * 3.333333 + 35 * 2.999999, 2))


@override_settings(ORS_API_KEY='test-key')
class RouteApiTests(TestCase):
    # Chicago -> St. Louis along a straight line (~258 mi), with ORS reporting 300 road miles.
    line = [[-87.63 + (-90.20 + 87.63) * i / 300, 41.88 + (38.63 - 41.88) * i / 300] for i in range(301)]

    def setUp(self):
        reset_station_index()
        self.addCleanup(reset_station_index)
        cache.clear()
        self.addCleanup(cache.clear)
        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)
        for opis_id, (lat, lng), price in [
            (1, (41.80, -87.70), '3.50'),
            (2, (40.25, -88.90), '3.00'),
            (3, (39.00, -89.90), '3.20'),
        ]:
            FuelStation.objects.create(
                opis_id=opis_id, name=f'S{opis_id}', address='I-55', city='Town', state='IL',
                price=Decimal(price), lat=lat, lng=lng,
            )
        patcher = mock.patch.object(ors._session, 'request')
        self.request = patcher.start()
        self.addCleanup(patcher.stop)
        self.request.return_value = _response(_directions_payload(self.line, 300 * 1609.344))

    def _post(self, payload):
        body = payload if isinstance(payload, str) else json.dumps(payload)
        return self.client.post(reverse('route'), body, content_type='application/json')

    def test_returns_route_plan(self):
        response = self._post({'start': 'Chicago, IL', 'finish': 'St. Louis, MO'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(
            set(data),
            {'start', 'finish', 'total_distance_miles', 'total_gallons', 'total_fuel_cost',
             'fuel_stops', 'route', 'map_url', 'external_api_calls'},
        )
        self.assertEqual(data['total_distance_miles'], 300.0)
        self.assertEqual(data['total_gallons'], 30.0)
        self.assertEqual(data['external_api_calls'], 1)  # both cities resolved locally
        self.assertEqual(data['route']['geometry']['type'], 'LineString')
        self.assertAlmostEqual(sum(s['gallons'] for s in data['fuel_stops']), 30.0, places=2)
        self.assertAlmostEqual(data['total_fuel_cost'], sum(s['cost'] for s in data['fuel_stops']), places=1)
        self.assertIn('/api/route/map/?start=Chicago', data['map_url'])

    def test_rejects_bad_input(self):
        for payload in ('not json', '[]', {'start': 'Chicago, IL'}, {'start': '', 'finish': 'X'},
                        {'start': 5, 'finish': 'X'}, {'start': 'Chicago, IL', 'finish': 'chicago, il'},
                        {'start': 'a' * 201, 'finish': 'X'}):
            response = self._post(payload)
            self.assertEqual(response.status_code, 400, payload)
            self.assertIn('error', response.json())
        self.request.assert_not_called()

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(reverse('route')).status_code, 405)

    def test_ors_failure_is_502(self):
        self.request.return_value = _response(status=503)
        response = self._post({'start': 'Chicago, IL', 'finish': 'St. Louis, MO'})
        self.assertEqual(response.status_code, 502)

    def test_no_fuel_in_range_is_422(self):
        FuelStation.objects.all().delete()
        response = self._post({'start': 'Chicago, IL', 'finish': 'St. Louis, MO'})
        self.assertEqual(response.status_code, 422)


    def test_repeat_request_is_served_from_cache(self):
        first = self._post({'start': 'Chicago, IL', 'finish': 'St. Louis, MO'}).json()
        self.assertEqual(self.request.call_count, 1)
        # Same trip with different spacing/case/abbreviation hits the cache.
        second = self._post({'start': ' chicago,  il ', 'finish': 'Saint Louis, MO'}).json()
        self.assertEqual(self.request.call_count, 1)
        self.assertEqual(second['external_api_calls'], 0)
        self.assertEqual(second['fuel_stops'], first['fuel_stops'])
        self.assertEqual(second['total_fuel_cost'], first['total_fuel_cost'])

    def test_errors_are_not_cached(self):
        self.request.return_value = _response(status=503)
        self._post({'start': 'Chicago, IL', 'finish': 'St. Louis, MO'})
        self.request.return_value = _response(_directions_payload(self.line, 300 * 1609.344))
        response = self._post({'start': 'Chicago, IL', 'finish': 'St. Louis, MO'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.request.call_count, 2)


    def test_map_reads_cached_result_without_calling_ors(self):
        data = self._post({'start': 'Chicago, IL', 'finish': 'St. Louis, MO'}).json()
        self.assertEqual(self.request.call_count, 1)
        response = self.client.get(data['map_url'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.request.call_count, 1)
        self.assertContains(response, 'leaflet@1.9.4')
        self.assertContains(response, 'id="map-data"')
        self.assertEqual(response.context['map_data']['fuel_stops'], data['fuel_stops'])

    def test_map_computes_once_when_not_cached(self):
        url = reverse('route-map') + '?start=Chicago,+IL&finish=St.+Louis,+MO'
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.request.call_count, 1)

    def test_map_escapes_user_input(self):
        self.request.return_value = _response(_directions_payload(self.line, 300 * 1609.344))
        with mock.patch.object(ors, 'lookup_city', return_value=(41.88, -87.63)):
            response = self.client.get(reverse('route-map'), {'start': '<script>x</script>, IL', 'finish': 'Peoria, IL'})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '<script>x</script>')

    def test_map_rejects_missing_params(self):
        response = self.client.get(reverse('route-map'), {'start': 'Chicago, IL'})
        self.assertEqual(response.status_code, 400)
        self.request.assert_not_called()


    def test_logs_time_of_each_step(self):
        logging.disable(logging.NOTSET)
        with self.assertLogs('routing.views', 'INFO') as logs:
            self._post({'start': 'Chicago, IL', 'finish': 'St. Louis, MO'})
        steps = [line.split(':')[2].strip() for line in logs.output]
        for step in ('geocode', 'ors directions', 'nearby search', 'optimizer'):
            self.assertIn(step, steps)
