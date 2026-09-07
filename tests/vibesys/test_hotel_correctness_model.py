# ruff: noqa: PT011, S106

import copy
import json
import struct

import pytest
from resources.evaluators.microservice.hotelcorrectness.catalog import (
    capacity_for_hotel,
    rate_backed_ids,
    seed_profiles,
)
from resources.evaluators.microservice.hotelcorrectness.model import (
    LOGIN_FAILURE,
    RESERVATION_FAILURE,
    RESERVATION_SUCCESS,
    SequentialModel,
    credentials_for_user,
)
from resources.evaluators.microservice.hotelcorrectness.schema import (
    decode_feature_collection,
    decode_feature_collection_json,
    exact_ids,
    validate_profiles,
)


def _feature(hotel_id: str) -> dict[str, object]:
    profile = seed_profiles()[hotel_id]

    def wire_value(value: float) -> float:
        for precision in range(1, 10):
            candidate = float(format(value, f".{precision}g"))
            round_trip = struct.unpack("f", struct.pack("f", candidate))[0]
            if round_trip == value:
                return candidate
        raise AssertionError

    return {
        "type": "Feature",
        "id": hotel_id,
        "properties": {"name": profile.name, "phone_number": profile.phone},
        "geometry": {
            "type": "Point",
            "coordinates": [wire_value(profile.lon), wire_value(profile.lat)],
        },
    }


def _reservation(**updates: str) -> dict[str, str]:
    username, password = credentials_for_user(0)
    query = {
        "hotelId": "1",
        "inDate": "3100-01-01",
        "outDate": "3100-01-02",
        "customerName": "model-test",
        "username": username,
        "password": password,
        "number": "200",
    }
    query.update(updates)
    return query


def test_catalog_matches_pinned_fixture_boundaries() -> None:
    catalog = seed_profiles()

    assert len(catalog) == 80
    assert (catalog["1"].name, catalog["1"].phone) == (
        "Clift Hotel",
        "(415) 775-4700",
    )
    assert (catalog["7"].name, catalog["7"].phone) == (
        "St. Regis San Francisco",
        "(415) 284-407",
    )
    assert catalog["80"].phone == "(415) 284-4080"
    assert (
        catalog["1"].lat,
        catalog["1"].lon,
        catalog["1"].recommend_lat,
        catalog["1"].recommend_lon,
    ) == (37.78670120239258, -122.41120147705078, 37.7867, -122.4112)
    assert (
        catalog["7"].lat,
        catalog["7"].lon,
        catalog["7"].recommend_lat,
        catalog["7"].recommend_lon,
    ) == (37.82550048828125, -122.35400390625, 37.8255, -122.354)
    assert (
        catalog["80"].lat,
        catalog["80"].lon,
        catalog["80"].recommend_lat,
        catalog["80"].recommend_lon,
    ) == (38.26350021362305, -121.77000427246094, 38.26349999999999, -121.77)
    assert {"1", "2", "3", "9", "78"} <= rate_backed_ids()
    assert not {"6", "7", "80"} & rate_backed_ids()
    assert [capacity_for_hotel(value) for value in ("1", "7", "8", "9")] == [
        200,
        300,
        250,
        200,
    ]
    assert credentials_for_user(0) == ("Cornell_30", "0000000000")
    assert credentials_for_user(132) == ("Cornell_313332", "132" * 10)


def test_strict_schema_and_catalog_reject_mutants() -> None:
    root = {"type": "FeatureCollection", "features": [_feature("1")]}
    features = decode_feature_collection(root)
    validate_profiles(features, seed_profiles())
    exact_ids(features, {"1"})

    mutants = []
    extra = copy.deepcopy(root)
    extra["extra"] = True
    mutants.append(extra)
    wrong_coordinate = copy.deepcopy(root)
    wrong_coordinate["features"][0]["geometry"]["coordinates"][0] = "-122"
    mutants.append(wrong_coordinate)
    duplicate = copy.deepcopy(root)
    duplicate["features"].append(copy.deepcopy(duplicate["features"][0]))
    mutants.append(duplicate)
    non_finite = copy.deepcopy(root)
    non_finite["features"][0]["geometry"]["coordinates"][0] = float("nan")
    mutants.append(non_finite)
    for mutant in mutants:
        with pytest.raises(ValueError):
            decode_feature_collection(mutant)

    wrong_profile = copy.deepcopy(root)
    wrong_profile["features"][0]["properties"]["name"] = "Impostor"
    with pytest.raises(ValueError, match="does not match"):
        validate_profiles(decode_feature_collection(wrong_profile), seed_profiles())


@pytest.mark.parametrize(
    ("hotel_id", "lon", "lat"),
    [
        ("2", -122.4005, 37.7854),
        ("27", -122.194, 37.9455),
        ("48", -122.026, 38.0715),
        ("63", -121.906006, 38.1615),
    ],
)
def test_profile_coordinates_normalize_at_float32_storage_boundary(
    hotel_id: str, lon: float, lat: float
) -> None:
    serialized = _feature(hotel_id)
    serialized["geometry"]["coordinates"] = [lon, lat]

    validate_profiles(decode_feature_collection({"type": "FeatureCollection", "features": [serialized]}), seed_profiles())

    # This nearby decimal rounds to the same float32, but Go's old rational
    # JSON comparison still rejected it because it was not the canonical wire value.
    serialized["geometry"]["coordinates"][1] = lat + 0.0000001
    with pytest.raises(ValueError, match="does not match"):
        validate_profiles(
            decode_feature_collection({"type": "FeatureCollection", "features": [serialized]}),
            seed_profiles(),
        )


def test_profile_coordinates_reject_same_float32_bin_and_float64_rounding_mutants() -> None:
    valid = json.dumps({"type": "FeatureCollection", "features": [_feature("48")]})
    validate_profiles(decode_feature_collection_json(valid), seed_profiles())

    same_float32_bin = valid.replace("38.0715", "38.07150001")
    beyond_float64_precision = valid.replace("38.0715", "38.0715000000000000000001")
    for mutant in (same_float32_bin, beyond_float64_precision):
        with pytest.raises(ValueError, match="does not match"):
            validate_profiles(decode_feature_collection_json(mutant), seed_profiles())


def test_model_preserves_capacity_atomicity_and_isolation() -> None:
    model = SequentialModel()

    assert model.apply({"kind": "reserve", **_reservation()}) == {"message": RESERVATION_SUCCESS}
    assert model.apply({"kind": "reserve", **_reservation(number="1")}) == {
        "message": RESERVATION_FAILURE
    }
    assert model.apply(
        {"kind": "reserve", **_reservation(inDate="3100-01-02", outDate="3100-01-03", number="201")}
    ) == {"message": RESERVATION_FAILURE}
    assert model.apply(
        {"kind": "reserve", **_reservation(inDate="3100-01-02", outDate="3100-01-03", number="1")}
    ) == {"message": RESERVATION_SUCCESS}
    assert model.apply({"kind": "reserve", **_reservation(hotelId="2", number="1")}) == {
        "message": RESERVATION_SUCCESS
    }


def test_invalid_auth_reservation_mutates_before_failure() -> None:
    model = SequentialModel()

    assert model.apply({"kind": "reserve", **_reservation(password="wrong")}) == {
        "message": LOGIN_FAILURE
    }
    assert model.apply({"kind": "reserve", **_reservation(number="1")}) == {
        "message": RESERVATION_FAILURE
    }


def test_omitted_number_consumes_zero_and_search_observes_exact_night() -> None:
    model = SequentialModel()
    omitted = _reservation()
    omitted.pop("number")

    assert model.apply({"kind": "reserve", "query": omitted}) == {"message": RESERVATION_SUCCESS}
    assert model.apply({"kind": "reserve", **_reservation()}) == {"message": RESERVATION_SUCCESS}
    filled = model.apply({"kind": "search", "inDate": "3100-01-01", "outDate": "3100-01-02"})
    adjacent = model.apply({"kind": "search", "inDate": "3100-01-02", "outDate": "3100-01-03"})
    assert "1" not in filled["hotel_ids"]
    assert "1" in adjacent["hotel_ids"]


def test_model_detects_ignored_acknowledged_reservation_mutant() -> None:
    correct = SequentialModel()
    mutant = SequentialModel()
    reservation = _reservation()
    correct.apply({"kind": "reserve", **reservation})

    search = {"kind": "search", "inDate": reservation["inDate"], "outDate": reservation["outDate"]}
    assert correct.apply(search) != mutant.apply(search)


def test_model_detects_hotel_and_date_coupling_mutants() -> None:
    correct = SequentialModel()
    coupled_hotel = SequentialModel()
    coupled_date = SequentialModel()
    reservation = _reservation()
    correct.apply({"kind": "reserve", **reservation})
    coupled_hotel.apply({"kind": "reserve", **reservation})
    coupled_date.apply({"kind": "reserve", **reservation})
    coupled_hotel.reserved[("2", reservation["inDate"])] = capacity_for_hotel("2")
    coupled_date.reserved[("1", "3100-01-02")] = capacity_for_hotel("1")

    other_hotel = {
        "kind": "reserve",
        **_reservation(hotelId="2", number="1"),
    }
    adjacent_date = {
        "kind": "search",
        "inDate": "3100-01-02",
        "outDate": "3100-01-03",
    }
    assert correct.apply(other_hotel) != coupled_hotel.apply(other_hotel)
    assert correct.apply(adjacent_date) != coupled_date.apply(adjacent_date)


def test_model_detects_removed_invalid_auth_side_effect_mutant() -> None:
    correct = SequentialModel()
    mutant = SequentialModel()
    invalid = _reservation(password="wrong")
    correct_response = correct.apply({"kind": "reserve", **invalid})
    # The mutant returns the same response but omits the pinned write side effect.
    mutant_response = {"message": LOGIN_FAILURE}
    assert correct_response == mutant_response
    readback = {"kind": "reserve", **_reservation(number="1")}
    assert correct.apply(readback) != mutant.apply(readback)


def test_model_detects_partial_write_on_atomic_rejection_mutant() -> None:
    correct = SequentialModel()
    mutant = SequentialModel()
    over = _reservation(number="201", inDate="3100-02-01", outDate="3100-02-02")
    assert correct.apply({"kind": "reserve", **over}) == {"message": RESERVATION_FAILURE}
    mutant.reserved[("1", "3100-02-01")] = 1

    fill = {"kind": "reserve", **_reservation(inDate="3100-02-01", outDate="3100-02-02")}
    assert correct.apply(fill) != mutant.apply(fill)
