import logging
import time

from django.apps import AppConfig
from django.db import DatabaseError

logger = logging.getLogger(__name__)


class RoutingConfig(AppConfig):
    name = 'routing'


def warm_caches():
    """Load the cities file and the station KD-tree once, when the server process starts."""
    from routing.services.cities import city_index
    from routing.services.stations import get_station_index

    started = time.perf_counter()
    city_index()
    try:
        stations = len(get_station_index().stations)
    except DatabaseError:
        logger.warning('Station table unavailable; run migrate and load_stations.')
        return
    logger.info('Warmed caches: %d stations in %.0f ms', stations, (time.perf_counter() - started) * 1000)
