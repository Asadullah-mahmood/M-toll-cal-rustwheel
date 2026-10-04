import pytest

from app.calculator import (InvalidJourney, RateNotAvailable, TollCalculator,
                            UnknownRoad, UnknownStation, UnknownVehicle)

c = TollCalculator()


def test_full_route_equals_notified_rate():
    r = c.calculate("M-2", "car", "Lahore", "Islamabad")
    assert r["base_toll"] == 1430 and r["total_toll"] == 1430


def test_direction_is_symmetric():
    a = c.calculate("m2", "car", "Lahore", "Chakwal")["total_toll"]
    b = c.calculate("M-2", "car", "Chakwal", "Lahore")["total_toll"]
    assert a == b


def test_partial_route_prorated_and_rounded():
    r = c.calculate("M-2", "car", "Lahore", "Sheikhupura")  # 41/367*1430 = 159.76
    assert r["base_toll"] == 160 and r["pricing_method"] == "distance_prorated"


def test_non_mtag_surcharge():
    r = c.calculate("M-1", "car", "Peshawar", "Islamabad", has_mtag=False)
    assert (r["base_toll"], r["non_mtag_surcharge"], r["total_toll"]) == (700, 350, 1050)


def test_flat_road_needs_no_stations():
    assert c.calculate("national highway", "wagon")["total_toll"] == 200
    assert c.calculate("N-55", "Car")["total_toll"] == 250


def test_aliases():
    assert c.calculate("M5", "Truck 2 3 axle", "Multan", "Sukkur")["total_toll"] == 5950
    assert c.calculate("M-14", "jeep", "Hakla", "Dera Ismail Khan")["total_toll"] == 800


def test_trip():
    t = c.calculate_trip([
        dict(motorway="M-2", vehicle_type="car", start="Lahore", end="Islamabad"),
        dict(motorway="M-1", vehicle_type="car", start="Islamabad", end="Peshawar"),
    ])
    assert t["total_toll"] == 1430 + 700


@pytest.mark.parametrize("kwargs,exc", [
    (dict(motorway="M-99", vehicle_type="car"), UnknownRoad),
    (dict(motorway="M-2", vehicle_type="spaceship", start="Lahore", end="Islamabad"), UnknownVehicle),
    (dict(motorway="M-2", vehicle_type="car", start="Karachi", end="Islamabad"), UnknownStation),
    (dict(motorway="M-2", vehicle_type="car", start="Lahore", end="Lahore"), InvalidJourney),
    (dict(motorway="M-2", vehicle_type="car", start="Lahore"), InvalidJourney),
    (dict(motorway="M-9", vehicle_type="bus", start="Karachi", end="Hyderabad"), RateNotAvailable),
])
def test_errors(kwargs, exc):
    with pytest.raises(exc):
        c.calculate(**kwargs)