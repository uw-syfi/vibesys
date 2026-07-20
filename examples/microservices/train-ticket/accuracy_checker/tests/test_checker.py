from __future__ import annotations

import random
from copy import deepcopy
from typing import Any

import pytest
from checker import (
    SEED_CATALOG,
    CheckFailure,
    admin_token,
    make_case,
    trip_key,
    validate_entity,
    verify_seed_catalog,
)


def test_random_case_is_internally_referentially_consistent() -> None:
    case = make_case(random.Random(7), "testnamespace", 3)

    assert case.route["stations"] == [case.station_a["id"], case.station_b["id"]]
    assert case.route["id"] == case.price["routeId"] == case.trip["routeId"]
    assert case.train["id"] == case.price["trainType"] == case.trip["trainTypeId"]
    assert trip_key(case.trip) == case.trip_input["tripId"]

    for service, entity in (
        ("config", case.config),
        ("station", case.station_a),
        ("station", case.station_b),
        ("train", case.train),
        ("route", case.route),
        ("price", case.price),
        ("travel", case.trip),
    ):
        assert validate_entity(service, entity, where=service) is entity


def test_schema_validation_rejects_missing_and_extra_fields() -> None:
    station = {"id": "x", "name": "X", "stayTime": 1}

    with pytest.raises(CheckFailure, match="fields"):
        validate_entity("station", {"id": "x", "name": "X"}, where="station")
    with pytest.raises(CheckFailure, match="fields"):
        validate_entity("station", {**station, "unexpected": True}, where="station")


def test_schema_validation_rejects_type_substitution() -> None:
    with pytest.raises(CheckFailure, match="stayTime"):
        validate_entity(
            "station",
            {"id": "x", "name": "X", "stayTime": "1"},
            where="station",
        )
    with pytest.raises(CheckFailure, match="tripId"):
        validate_entity(
            "travel",
            {
                "tripId": "G1234",
                "trainTypeId": "GaoTieOne",
                "routeId": "route",
                "startingTime": 1,
                "startingStationId": "a",
                "stationsId": "b",
                "terminalStationId": "c",
                "endTime": 2,
            },
            where="trip",
        )


def test_admin_token_is_runtime_bound() -> None:
    first = admin_token(now=1_000)
    second = admin_token(now=1_001)

    assert first != second
    assert len(first.split(".")) == 3


def test_seed_catalog_checks_every_value() -> None:
    class CatalogClient:
        def __init__(self, catalog: dict[str, list[dict[str, Any]]]) -> None:
            self.catalog = catalog

        def list_entities(self, service: str) -> list[dict[str, Any]]:
            return self.catalog[service]

    assert verify_seed_catalog(CatalogClient(deepcopy(SEED_CATALOG))) == 45  # type: ignore[arg-type]

    corrupted = deepcopy(SEED_CATALOG)
    corrupted["price"][7]["basicPriceRate"] = 999.0
    with pytest.raises(CheckFailure, match="seed price"):
        verify_seed_catalog(CatalogClient(corrupted))  # type: ignore[arg-type]
