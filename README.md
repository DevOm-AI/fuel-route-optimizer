# Fuel Route Optimizer

A Django API that takes a start and finish in the US, gets the driving route, and picks the cheapest fuel stops for a car with a 500-mile range. It also returns the total fuel cost at 10 MPG.
A new route costs one OpenRouteService (ORS) call. A place that isn't in the local cities file costs one more call to geocode it.

## Setup

I built this on Python 3.12.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then put your ORS key in ORS_API_KEY
python manage.py migrate
python manage.py load_stations
python manage.py runserver
```

## Usage

```bash
curl -X POST http://127.0.0.1:8000/api/route/ \
  -H 'Content-Type: application/json' \
  -d '{"start": "New York, NY", "finish": "Los Angeles, CA"}'
```

Here is the real response for that call. I kept the first 2 of its 17 fuel stops and cut the route coordinates (the full line has 21,924 points):

```json
{
  "start": {"query": "New York, NY", "lat": 40.6943, "lng": -73.9249},
  "finish": {"query": "Los Angeles, CA", "lat": 34.1141, "lng": -118.4068},
  "total_distance_miles": 2809.3,
  "total_gallons": 280.93,
  "total_fuel_cost": 852.18,
  "fuel_stops": [
    {
      "type": "start",
      "name": "Trip start",
      "address": "New York, NY",
      "city": null,
      "state": null,
      "lat": 40.6943,
      "lng": -73.9249,
      "priced_as": "BOLLA MARKET",
      "price": 3.099,
      "mile_marker": 0.0,
      "gallons": 7.504,
      "cost": 23.26
    },
    {
      "type": "station",
      "name": "ACI TRUCK STOP",
      "address": "US-46",
      "city": "Columbia",
      "state": "NJ",
      "lat": 40.926,
      "lng": -75.0945,
      "price": 3.079,
      "mile_marker": 75.0,
      "gallons": 32.398,
      "cost": 99.75
    }
  ],
  "route": {
    "type": "Feature",
    "geometry": {"type": "LineString", "coordinates": [[-73.924563, 40.69411], "..."]},
    "properties": {}
  },
  "external_api_calls": 1,
  "map_url": "http://127.0.0.1:8000/api/route/map/?start=New+York%2C+NY&finish=Los+Angeles%2C+CA"
}
```

Open `map_url` in a browser to see the route and stops on a Leaflet map.

There is a Postman collection at `postman/fuel-route-optimizer.postman_collection.json`.
Import it in Postman and run the requests in order.

Errors come back as `{"error": "..."}`:

- `400`: bad JSON, missing or empty `start`/`finish`, a value over 200 characters, same start and finish, or a place that can't be found or is outside the US.
- `422`: some stretch of the route is longer than 500 miles with no station near it.
- `502`: ORS failed or timed out, or `ORS_API_KEY` is not set.

## How it works

1. Look up each place ("City, ST" or "City, State") in `data/uscities.csv`. If it isn't there, ask the ORS geocoder, limited to the US.
2. Make one ORS driving directions call to get the road line and distance.
3. Thin the line to about one point per mile and query a KD-tree of all stations for anything within 10 miles. Each station gets the mile marker of the closest route point.
4. Run the greedy optimizer. At each stop: if a cheaper station is within 500 miles, buy just enough to reach it. Otherwise, if the finish is within 500 miles, buy just enough to finish. Otherwise, fill up and drive to the cheapest station in range.
5. Cache the result for 24 hours, keyed on the normalized start and finish. A repeat request makes no external calls and returns `"external_api_calls": 0`.

The server loads the cities file and builds the station KD-tree once at startup, not on each request.

## Why greedy

With a fixed route and no extra cost per stop, this is the "gas station problem", and the greedy rule above gives the minimum total cost. I didn't want to rely on that claim alone, so a test runs the optimizer on 100 random routes and checks each result against a brute-force DP over fuel levels.

## Assumptions

- The fuel CSV has no coordinates, so I put each station at the center of its city, matched by city and state against the SimpleMaps US cities file. `load_stations` reads 8,151 rows, which come down to 6,626 unique US stations. 6,158 of them match a city and the other 468 are skipped.
- The route is fixed. I ignore the detour from the road to a station.
- The tank starts empty. The first fill happens at the start and is priced like the first station on the route. The tank ends empty.
- The goal is the lowest cost, not the fewest stops. This is why the example has 17 stops, some only a gallon or two.
- When a station ID appears more than once, I keep the lowest price. Canadian rows are dropped.

## Tests

```bash
python manage.py test
```

There are 55 tests. They cover CSV cleaning and city matching, geocoding, the station search, the optimizer (including the brute-force check), and the API's errors, caching and map view. ORS is mocked, so the tests don't need a key.

## What I'd do next

- Add a fixed cost per stop and switch to a DP, so the planner stops making 1-gallon stops.
- Swap LocMemCache for Redis so the cache is shared across workers.
- Geocode each station by its address so stations aren't stacked on city centers.
- Move to PostGIS if the station list grows well past the current 6,158.
