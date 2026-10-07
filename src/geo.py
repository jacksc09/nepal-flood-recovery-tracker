"""Point-in-polygon lookups against the full-detail corridor boundaries.

build_boundaries.py writes data/raw/cod/corridor_full.geojson (the COD-AB
districts and municipalities of the corridor, unsimplified). This module
answers "which municipality is this point in?", which is how town
coordinates, BIPAD centroids and Copernicus damage areas are tied to a
pcode: by checking the map, never by guessing from a name.
"""

import json
from functools import lru_cache

from shapely.geometry import Point, shape
from shapely.strtree import STRtree

from config import RAW

CORRIDOR_FULL = RAW / "cod" / "corridor_full.geojson"


@lru_cache(maxsize=1)
def _index() -> tuple:
    if not CORRIDOR_FULL.exists():
        raise FileNotFoundError(f"{CORRIDOR_FULL} is missing: run "
                                ".venv/bin/python src/build_boundaries.py first")
    features = json.loads(CORRIDOR_FULL.read_text())["features"]
    munis = [f for f in features if f["properties"]["level"] == "municipality"]
    shapes = [shape(f["geometry"]) for f in munis]
    return STRtree(shapes), shapes, [f["properties"] for f in munis]


def municipality_at(lon: float, lat: float):
    """Properties of the corridor municipality containing the point, or None."""
    tree, shapes, props = _index()
    point = Point(lon, lat)
    for i in tree.query(point):
        if shapes[i].covers(point):
            return props[i]
    return None
