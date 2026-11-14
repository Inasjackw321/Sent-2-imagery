"""The footprint tests that replace shapely: coverage and touching."""

import pytest

from backend.changemap import geometry


def square(w, s, e, n):
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


AOI = (30.0, 50.0, 31.0, 51.0)


def test_full_half_and_no_coverage():
    assert geometry.coverage([square(29, 49, 32, 52)], AOI) == 1.0
    assert geometry.coverage([square(29, 49, 30.5, 52)], AOI) == pytest.approx(0.5, abs=0.02)
    assert geometry.coverage([square(40, 40, 41, 41)], AOI) == 0.0


def test_two_halves_make_a_whole():
    halves = [square(29, 49, 30.5, 52), square(30.5, 49, 32, 52)]
    assert geometry.coverage(halves, AOI) == 1.0


def test_a_tilted_swath():
    swath = {"type": "Polygon", "coordinates": [[[29.5, 49], [31.5, 49.2], [31.3, 52], [29.3, 51.8],
                                                 [29.5, 49]]]}
    assert geometry.coverage([swath], AOI) == 1.0


def test_holes_and_multipolygons():
    donut = {"type": "Polygon", "coordinates": [
        [[29, 49], [32, 49], [32, 52], [29, 52], [29, 49]],
        [[30.25, 50.25], [30.75, 50.25], [30.75, 50.75], [30.25, 50.75], [30.25, 50.25]]]}
    assert geometry.coverage([donut], AOI) == pytest.approx(0.75, abs=0.02)
    multi = {"type": "MultiPolygon", "coordinates": [square(29, 49, 30.5, 52)["coordinates"],
                                                     square(30.5, 49, 32, 52)["coordinates"]]}
    assert geometry.coverage([multi], AOI) == 1.0


@pytest.mark.parametrize("geom, expected", [
    (square(29, 49, 32, 52), True),        # contains the window
    (square(30.4, 50.4, 30.6, 50.6), True),  # inside the window
    (square(30.9, 49, 32, 52), True),       # overlaps an edge
    (square(29.5, 50.4, 31.5, 50.6), True),  # crosses it, no corners inside either way
    (square(32, 52, 33, 53), False),
])
def test_touches(geom, expected):
    assert geometry.touches(geom, AOI) is expected
