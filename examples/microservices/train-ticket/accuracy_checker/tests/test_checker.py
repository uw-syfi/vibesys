from __future__ import annotations

import random
import signal
from copy import deepcopy
from pathlib import Path
from typing import Any

import checker as checker_module
import pytest
from checker import (
    SEED_CATALOG,
    CheckFailure,
    ManagedCandidate,
    admin_token,
    index_entities,
    make_case,
    trip_key,
    update_case,
    validate_entity,
    verify_retired_station_names,
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

    duplicated = deepcopy(SEED_CATALOG)
    duplicated["station"].append(deepcopy(duplicated["station"][0]))
    with pytest.raises(CheckFailure, match="seed count"):
        verify_seed_catalog(CatalogClient(duplicated))  # type: ignore[arg-type]


def test_index_entities_rejects_duplicate_keys() -> None:
    station = {"id": "duplicate", "name": "A", "stayTime": 1}

    with pytest.raises(CheckFailure, match="duplicate key"):
        index_entities("station", [station, {**station, "name": "B"}])


def test_station_updates_probe_retired_secondary_index_names() -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.negative_reads: list[tuple[str, str, str, int]] = []

        def envelope(
            self,
            service: str,
            method: str,
            path: str,
            body: Any | None = None,
            *,
            app_status: int = 1,
            **_: Any,
        ) -> dict[str, Any]:
            if method == "GET":
                self.negative_reads.append((service, method, path, app_status))
            return {"status": app_status, "msg": "ok", "data": body}

    case = make_case(random.Random(7), "namespace", 0)
    old_names = {case.station_a["name"], case.station_b["name"]}
    client = RecordingClient()
    update_case(client, case, random.Random(8))  # type: ignore[arg-type]
    verify_retired_station_names(client, case)  # type: ignore[arg-type]

    assert set(case.retired_station_names) == old_names
    assert len(client.negative_reads) == 2
    assert all(read[0:2] == ("station", "GET") and read[3] == 0 for read in client.negative_reads)


def test_managed_candidate_starts_and_kills_a_process_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeProcess:
        pid = 4312

        def wait(self, timeout: float) -> int:
            assert timeout == 5
            return 0

    popen_arguments: dict[str, Any] = {}
    process = FakeProcess()

    def fake_popen(*args: Any, **kwargs: Any) -> FakeProcess:
        popen_arguments.update(kwargs)
        return process

    group_alive = True
    delivered_signals: list[signal.Signals] = []

    def fake_killpg(process_group: int, requested_signal: signal.Signals | int) -> None:
        nonlocal group_alive
        assert process_group == process.pid
        if requested_signal == 0:
            if not group_alive:
                raise ProcessLookupError
            return
        delivered_signals.append(signal.Signals(requested_signal))
        if requested_signal == signal.SIGKILL:
            group_alive = False

    monkeypatch.setattr(checker_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(checker_module, "wait_ready", lambda *_: None)
    monkeypatch.setattr(checker_module.os, "killpg", fake_killpg)

    managed = ManagedCandidate(["./run.sh"], tmp_path, tmp_path, object(), 1)  # type: ignore[arg-type]
    try:
        managed.start()
        managed.stop(kill=True)
    finally:
        managed.close()

    assert popen_arguments["start_new_session"] is True
    assert delivered_signals == [signal.SIGKILL]
