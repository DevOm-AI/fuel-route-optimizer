import numpy as np

EARTH_RADIUS_MILES = 3958.8


def haversine_miles(lat1, lng1, lat2, lng2):
    """Great-circle distance in miles; accepts scalars or numpy arrays."""
    lat1, lng1, lat2, lng2 = map(np.radians, (lat1, lng1, lat2, lng2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lng2 - lng1) / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(a))


def mile_markers(coordinates):
    """Cumulative miles from the start for each [lng, lat] point on a line."""
    points = np.asarray(coordinates, dtype=float)
    if len(points) == 0:
        return np.zeros(0)
    lng, lat = points[:, 0], points[:, 1]
    steps = haversine_miles(lat[:-1], lng[:-1], lat[1:], lng[1:])
    return np.concatenate(([0.0], np.cumsum(steps)))
