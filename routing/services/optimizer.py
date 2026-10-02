"""Greedy fuel stop planner. Pure Python: no Django, easy to test.

Fuel is tracked in "miles of range" (gallons * mpg) so the tank size is simply range_miles.
"""

from dataclasses import dataclass
from typing import Any

EPSILON = 1e-9


class NoFuelInRangeError(Exception):
    """The route has a gap longer than the vehicle range with no station in it."""


@dataclass(frozen=True)
class FuelOption:
    mile: float  # position along the route
    price: float  # per gallon
    data: Any = None  # caller payload (e.g. the station); None for the virtual start


@dataclass(frozen=True)
class Purchase:
    option: FuelOption
    gallons: float
    cost: float


def plan_fuel_stops(options, total_miles, range_miles=500.0, mpg=10.0):
    """Cheapest purchases to drive total_miles, starting and ending with an empty tank.

    A virtual station at mile 0 is priced like the station nearest the start. At each stop:
    if a cheaper station is within range, buy just enough to reach the nearest one; else if
    the finish is within range, buy just enough to finish; else fill up and go to the
    cheapest station in range. Raises NoFuelInRangeError when no station is in range.
    """
    if total_miles <= EPSILON:
        return []

    stations = sorted(
        (o for o in options if -EPSILON <= o.mile <= total_miles + EPSILON),
        key=lambda o: o.mile,
    )
    if not stations:
        raise NoFuelInRangeError('No fuel station found along the route.')

    route = [FuelOption(0.0, stations[0].price), *stations]
    purchases = []
    fuel = 0.0
    i = 0

    def buy(option, miles):
        if miles > EPSILON:
            gallons = miles / mpg
            purchases.append(Purchase(option, gallons, gallons * option.price))

    while True:
        current = route[i]
        limit = current.mile + range_miles + EPSILON

        cheaper = cheapest = None
        j = i + 1
        while j < len(route) and route[j].mile <= limit:
            if route[j].price < current.price:
                cheaper = j
                break
            if cheapest is None or route[j].price <= route[cheapest].price:
                cheapest = j
            j += 1

        if cheaper is not None:
            needed = route[cheaper].mile - current.mile
            buy(current, needed - fuel)
            fuel = max(fuel, needed) - needed
            i = cheaper
            continue

        to_finish = total_miles - current.mile
        if to_finish <= range_miles + EPSILON:
            buy(current, to_finish - fuel)
            return purchases

        if cheapest is None:
            raise NoFuelInRangeError(
                f'No fuel within {range_miles:g} miles after mile {current.mile:.0f}.'
            )
        buy(current, range_miles - fuel)
        fuel = range_miles - (route[cheapest].mile - current.mile)
        i = cheapest
