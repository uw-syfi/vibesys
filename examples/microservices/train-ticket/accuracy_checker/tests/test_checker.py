from __future__ import annotations

import random
import signal
import sys
import time
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
    create_case,
    index_entities,
    make_case,
    trip_key,
    update_case,
    validate_entity,
    verify_deleted,
    verify_retired_secondary_indexes,
    verify_retired_station_names,
    verify_seed_catalog,
    verify_seed_catalog_present,
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


def test_admin_token_has_random_mode_neutral_identity() -> None:
    first = admin_token(now=1_000)
    second = admin_token(now=1_000)

    assert first != second
    payload = first.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims = checker_module.json.loads(checker_module.base64.urlsafe_b64decode(payload))
    assert claims["sub"] == claims["id"]
    assert len(claims["sub"]) == 24
    int(claims["sub"], 16)
    assert "checker" not in claims["sub"] and "benchmark" not in claims["sub"]


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

    over_deleted = deepcopy(SEED_CATALOG)
    over_deleted["train"].pop()
    with pytest.raises(CheckFailure, match="disappeared after a runtime delete"):
        verify_seed_catalog_present(CatalogClient(over_deleted))  # type: ignore[arg-type]


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


def test_route_and_price_updates_probe_retired_secondary_keys() -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.reads: list[tuple[str, str, int]] = []

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
                self.reads.append((service, path, app_status))
            return {"status": app_status, "msg": "ok", "data": [] if service == "route" else body}

    first = make_case(random.Random(7), "0123456789abcdef01234567", 0)
    old_route_key = (first.route["startStationId"], first.route["terminalStationId"])
    old_price_key = (first.price["routeId"], first.price["trainType"])
    client = RecordingClient()

    update_case(client, first, random.Random(9))  # type: ignore[arg-type]
    verify_retired_secondary_indexes(client, first)  # type: ignore[arg-type]

    assert first.retired_route_keys == [old_route_key]
    assert first.retired_price_keys == [old_price_key]
    assert ("route", f"/routes/{old_route_key[0]}/{old_route_key[1]}", 0) in client.reads
    assert ("price", f"/prices/{old_price_key[0]}/{old_price_key[1]}", 0) in client.reads


def test_verify_deleted_rejects_non_station_entity_retained_only_in_list() -> None:
    class PhantomListClient:
        def envelope(self, *_: Any, app_status: int = 1, **__: Any) -> dict[str, Any]:
            return {"status": app_status, "msg": "ok", "data": None}

        def list_entities(self, service: str) -> list[dict[str, Any]]:
            return [case.train] if service == "train" else []

    case = make_case(random.Random(7), "tt0123456789abcdef01234567", 0)
    with pytest.raises(CheckFailure, match="deleted train .* remains visible in list"):
        verify_deleted(PhantomListClient(), case)  # type: ignore[arg-type]


def test_partial_case_creation_is_cleaned_up() -> None:
    class FailingCreateClient:
        def __init__(self) -> None:
            self.creates = 0
            self.cleanup_requests: list[tuple[str, str, str]] = []

        def envelope(self, service: str, method: str, path: str, *_: Any, **__: Any) -> Any:
            self.creates += 1
            if self.creates == 3:
                raise CheckFailure("injected create failure")
            return {"status": 1, "msg": "ok", "data": None}

        def request(self, service: str, method: str, path: str, *_: Any, **__: Any) -> None:
            self.cleanup_requests.append((service, method, path))

    client = FailingCreateClient()
    case = make_case(random.Random(7), "tt0123456789abcdef01234567", 0)
    with pytest.raises(CheckFailure, match="injected create failure"):
        create_case(client, case)  # type: ignore[arg-type]

    assert len(client.cleanup_requests) == 7
    assert all(method == "DELETE" for _, method, _ in client.cleanup_requests)


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
    monkeypatch.setattr(checker_module, "descendant_processes", lambda *_: set())
    monkeypatch.setattr(checker_module.os, "killpg", fake_killpg)

    managed = ManagedCandidate(["./run.sh"], tmp_path, tmp_path, object(), 1)  # type: ignore[arg-type]
    try:
        managed.start()
        managed.stop(kill=True)
    finally:
        managed.close()

    assert popen_arguments["start_new_session"] is True
    assert delivered_signals == [signal.SIGKILL]


@pytest.mark.skipif(sys.platform != "linux", reason="detached-child probe uses Linux /proc")
def test_managed_candidate_kills_an_escaping_detached_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    child_code = (
        "import os,sys,time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)"
    )
    launcher_code = (
        "import os,subprocess,sys,time; "
        "subprocess.Popen([sys.executable, '-c', sys.argv[2], sys.argv[1]], "
        "start_new_session=True); "
        "deadline=time.time()+5; "
        "\nwhile not os.path.exists(sys.argv[1]) and time.time()<deadline: time.sleep(0.01); "
        "\ntime.sleep(60)"
    )

    class ProcessProbeClient:
        def request(self, *_: Any, **__: Any) -> Any:
            if not pid_file.exists():
                raise OSError("child has not started")
            child_pid = int(pid_file.read_text())
            stat_path = Path(f"/proc/{child_pid}/stat")
            try:
                process_state = stat_path.read_text().split()[2]
            except (FileNotFoundError, ProcessLookupError) as exc:
                raise OSError("child stopped") from exc
            if process_state == "Z":
                raise OSError("child stopped")
            return checker_module.HTTPResult(status=200, headers={}, raw=b"", json=None)

    managed = ManagedCandidate(
        [sys.executable, "-c", launcher_code, str(pid_file), child_code],
        tmp_path,
        tmp_path,
        ProcessProbeClient(),  # type: ignore[arg-type]
        5,
    )
    child_pid: int | None = None
    try:
        managed.start()
        child_pid = int(pid_file.read_text())
        managed.stop(kill=True)
        with pytest.raises(OSError, match="child stopped"):
            ProcessProbeClient().request()
    finally:
        managed.close()
        if child_pid is not None:
            try:
                checker_module.os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.05)
