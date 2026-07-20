#!/usr/bin/env python3
"""Black-box stateful correctness checker for Train Ticket v0.2.0 behavior."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client
import json
import os
import random
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any

SERVICE_PATHS = {
    "config": "/api/v1/configservice",
    "station": "/api/v1/stationservice",
    "train": "/api/v1/trainservice",
    "travel": "/api/v1/travelservice",
    "route": "/api/v1/routeservice",
    "price": "/api/v1/priceservice",
}

WELCOME_PATHS = {
    "config": ("/welcome", "Welcome to [ Config Service ] !"),
    "station": ("/welcome", "Welcome to [ Station Service ] !"),
    "train": ("/trains/welcome", "Welcome to [ Train Service ] !"),
    "travel": ("/welcome", "Welcome to [ Travel Service ] !"),
    "route": ("/welcome", "Welcome to [ Route Service ] !"),
    "price": ("/prices/welcome", "Welcome to [ Price Service ] !"),
}

LIST_PATHS = {
    "config": "/configs",
    "station": "/stations",
    "train": "/trains",
    "travel": "/trips",
    "route": "/routes",
    "price": "/prices",
}

ENTITY_FIELDS = {
    "config": {"name", "value", "description"},
    "station": {"id", "name", "stayTime"},
    "train": {"id", "economyClass", "confortClass", "averageSpeed"},
    "travel": {
        "tripId",
        "trainTypeId",
        "routeId",
        "startingTime",
        "startingStationId",
        "stationsId",
        "terminalStationId",
        "endTime",
    },
    "route": {"id", "stations", "distances", "startStationId", "terminalStationId"},
    "price": {
        "id",
        "trainType",
        "routeId",
        "basicPriceRate",
        "firstClassPriceRate",
    },
}

SEED_CATALOG: dict[str, list[dict[str, Any]]] = json.loads(
    (Path(__file__).with_name("seed_catalog.json")).read_text(encoding="utf-8")
)


class CheckFailure(RuntimeError):
    """Raised when an externally observable correctness property is violated."""


@dataclass(frozen=True)
class HTTPResult:
    status: int
    headers: Mapping[str, str]
    raw: bytes
    json: Any | None


@dataclass
class GraphCase:
    config: dict[str, Any]
    station_a: dict[str, Any]
    station_b: dict[str, Any]
    train: dict[str, Any]
    route_input: dict[str, Any]
    route: dict[str, Any]
    price: dict[str, Any]
    trip_input: dict[str, Any]
    trip: dict[str, Any]
    retired_station_names: dict[str, str] = dataclass_field(default_factory=dict)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def admin_token(now: int | None = None) -> str:
    issued = int(time.time()) if now is None else now
    header = _b64url(b'{"alg":"HS256","typ":"JWT"}')
    claims = _b64url(
        json.dumps(
            {
                "sub": "vibesys-checker",
                "roles": ["ROLE_ADMIN"],
                "id": "vibesys-checker",
                "iat": issued,
                "exp": issued + 3600,
            },
            separators=(",", ":"),
        ).encode("utf-8")
    )
    signing_input = f"{header}.{claims}".encode("ascii")
    signature = _b64url(hmac.new(b"secret", signing_input, hashlib.sha256).digest())
    return f"{header}.{claims}.{signature}"


class APIClient:
    def __init__(self, base_url: str, targets: Mapping[str, str], timeout: float) -> None:
        default = base_url.rstrip("/")
        self._targets = {name: targets.get(name, default).rstrip("/") for name in SERVICE_PATHS}
        self._timeout = timeout
        self._token = admin_token()

    def url(self, service: str, path: str) -> str:
        return self._targets[service] + SERVICE_PATHS[service] + path

    def request(
        self,
        service: str,
        method: str,
        path: str,
        body: Any | None = None,
        *,
        authenticated: bool = True,
    ) -> HTTPResult:
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = {"Accept": "application/json,text/plain,*/*"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if authenticated:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(
            self.url(service, path), data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                status = response.status
                response_headers = {key.lower(): value for key, value in response.headers.items()}
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            response_headers = {key.lower(): value for key, value in exc.headers.items()}
            raw = exc.read()
        parsed = None
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
        return HTTPResult(status=status, headers=response_headers, raw=raw, json=parsed)

    def envelope(
        self,
        service: str,
        method: str,
        path: str,
        body: Any | None = None,
        *,
        http_status: int = 200,
        app_status: int = 1,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        result = self.request(service, method, path, body, authenticated=authenticated)
        if result.status != http_status:
            raise CheckFailure(
                f"{method} {service}{path}: HTTP {result.status}, expected {http_status}; "
                f"body={result.raw[:300]!r}"
            )
        if not isinstance(result.json, dict) or set(result.json) != {"status", "msg", "data"}:
            raise CheckFailure(
                f"{method} {service}{path}: expected exact response envelope, got {result.json!r}"
            )
        if result.json["status"] != app_status:
            raise CheckFailure(
                f"{method} {service}{path}: application status {result.json['status']!r}, "
                f"expected {app_status}; envelope={result.json!r}"
            )
        return result.json

    def list_entities(self, service: str) -> list[dict[str, Any]]:
        envelope = self.envelope(service, "GET", LIST_PATHS[service])
        data = envelope["data"]
        if not isinstance(data, list):
            raise CheckFailure(f"GET {service}{LIST_PATHS[service]}: data must be a list")
        for index, item in enumerate(data):
            validate_entity(service, item, where=f"{service} list item {index}")
        return data


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)) or isinstance(value, float)


def trip_key(item: Mapping[str, Any]) -> str:
    trip_id = item.get("tripId")
    if not isinstance(trip_id, dict) or set(trip_id) != {"type", "number"}:
        raise CheckFailure(f"tripId must be {{type, number}}, got {trip_id!r}")
    train_type = trip_id["type"]
    number = trip_id["number"]
    if train_type not in {"G", "D"} or not isinstance(number, str) or not number:
        raise CheckFailure(f"invalid tripId components: {trip_id!r}")
    return train_type + number


def entity_key(service: str, item: Mapping[str, Any]) -> str:
    if service == "config":
        return str(item["name"])
    if service == "travel":
        return trip_key(item)
    return str(item["id"])


def validate_entity(service: str, item: Any, *, where: str) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise CheckFailure(f"{where}: expected object, got {type(item).__name__}")
    expected_fields = ENTITY_FIELDS[service]
    if set(item) != expected_fields:
        raise CheckFailure(f"{where}: fields {sorted(item)} do not match {sorted(expected_fields)}")
    string_fields = {
        "config": ("name", "value", "description"),
        "station": ("id", "name"),
        "train": ("id",),
        "travel": (
            "trainTypeId",
            "routeId",
            "startingStationId",
            "stationsId",
            "terminalStationId",
        ),
        "route": ("id", "startStationId", "terminalStationId"),
        "price": ("id", "trainType", "routeId"),
    }[service]
    for field in string_fields:
        if not isinstance(item[field], str):
            raise CheckFailure(f"{where}.{field}: expected string, got {item[field]!r}")
    if service == "station" and not _is_int(item["stayTime"]):
        raise CheckFailure(f"{where}.stayTime must be an integer")
    if service == "train":
        for field in ("economyClass", "confortClass", "averageSpeed"):
            if not _is_int(item[field]):
                raise CheckFailure(f"{where}.{field} must be an integer")
    if service == "route":
        if not isinstance(item["stations"], list) or not all(
            isinstance(value, str) for value in item["stations"]
        ):
            raise CheckFailure(f"{where}.stations must be a string list")
        if not isinstance(item["distances"], list) or not all(
            _is_int(value) for value in item["distances"]
        ):
            raise CheckFailure(f"{where}.distances must be an integer list")
        if len(item["stations"]) != len(item["distances"]):
            raise CheckFailure(f"{where}: stations and distances lengths differ")
    if service == "price":
        for field in ("basicPriceRate", "firstClassPriceRate"):
            if not _is_number(item[field]):
                raise CheckFailure(f"{where}.{field} must be numeric")
    if service == "travel":
        trip_key(item)
        for field in ("startingTime", "endTime"):
            if not _is_int(item[field]):
                raise CheckFailure(f"{where}.{field} must be epoch milliseconds")
    return item


def make_case(rng: random.Random, namespace: str, index: int) -> GraphCase:
    token = f"{namespace}{index:x}{rng.getrandbits(32):08x}"
    station_a = {"id": token + "a", "name": "A " + token, "stayTime": rng.randint(1, 40)}
    station_b = {"id": token + "b", "name": "B " + token, "stayTime": rng.randint(1, 40)}
    train = {
        "id": "T" + token,
        "economyClass": rng.randint(100, 900),
        "confortClass": rng.randint(50, 300),
        "averageSpeed": rng.randint(80, 350),
    }
    route_id = str(uuid.UUID(int=rng.getrandbits(128), version=4))
    distance = rng.randint(100, 1800)
    route_input = {
        "id": route_id,
        "startStation": station_a["id"],
        "endStation": station_b["id"],
        "stationList": f"{station_a['id']},{station_b['id']}",
        "distanceList": f"0,{distance}",
    }
    route = {
        "id": route_id,
        "stations": [station_a["id"], station_b["id"]],
        "distances": [0, distance],
        "startStationId": station_a["id"],
        "terminalStationId": station_b["id"],
    }
    price = {
        "id": str(uuid.UUID(int=rng.getrandbits(128), version=4)),
        "trainType": train["id"],
        "routeId": route_id,
        "basicPriceRate": round(rng.uniform(0.1, 0.9), 4),
        "firstClassPriceRate": round(rng.uniform(0.9, 1.9), 4),
    }
    trip_type = rng.choice(("G", "D"))
    trip_number = f"{rng.randint(1000000, 9999999)}"
    trip_id = trip_type + trip_number
    starting_time = rng.randint(1_600_000_000_000, 1_900_000_000_000)
    end_time = starting_time + rng.randint(3_600_000, 43_200_000)
    trip_input = {
        "tripId": trip_id,
        "trainTypeId": train["id"],
        "routeId": route_id,
        "startingStationId": station_a["id"],
        "stationsId": station_b["id"],
        "terminalStationId": station_b["id"],
        "startingTime": starting_time,
        "endTime": end_time,
    }
    trip = {**trip_input, "tripId": {"type": trip_type, "number": trip_number}}
    config = {
        "name": token + "Config",
        "value": secrets.token_urlsafe(18),
        "description": "generated " + secrets.token_urlsafe(22),
    }
    return GraphCase(
        config=config,
        station_a=station_a,
        station_b=station_b,
        train=train,
        route_input=route_input,
        route=route,
        price=price,
        trip_input=trip_input,
        trip=trip,
    )


def assert_entity(service: str, actual: Any, expected: Mapping[str, Any], *, where: str) -> None:
    item = validate_entity(service, actual, where=where)
    if item != expected:
        raise CheckFailure(
            f"{where}: entity mismatch\nactual={item!r}\nexpected={dict(expected)!r}"
        )


def index_entities(
    service: str, entities: Iterable[Mapping[str, Any]]
) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(entities):
        key = entity_key(service, item)
        if key in indexed:
            raise CheckFailure(f"{service} list contains duplicate key {key!r} at item {index}")
        indexed[key] = item
    return indexed


def verify_seed_catalog(client: APIClient) -> int:
    checks = 0
    for service, expected_entities in SEED_CATALOG.items():
        expected = index_entities(service, expected_entities)
        actual_entities = client.list_entities(service)
        if len(actual_entities) != len(expected_entities):
            raise CheckFailure(
                f"{service} seed count is {len(actual_entities)}, "
                f"expected exactly {len(expected_entities)}"
            )
        actual = index_entities(service, actual_entities)
        if actual.keys() != expected.keys():
            raise CheckFailure(
                f"{service} seed IDs differ: missing={sorted(expected.keys() - actual.keys())}, "
                f"unexpected={sorted(actual.keys() - expected.keys())}"
            )
        for key, expected_entity in expected.items():
            assert_entity(
                service,
                actual[key],
                expected_entity,
                where=f"seed {service} {key}",
            )
            checks += 1
    return checks


def verify_welcomes_and_http(client: APIClient) -> int:
    checks = 0
    for service, (path, expected) in WELCOME_PATHS.items():
        result = client.request(service, "GET", path)
        if result.status != 200 or result.raw.decode("utf-8") != expected:
            raise CheckFailure(
                f"{service} welcome mismatch: HTTP {result.status}, body={result.raw!r}"
            )
        checks += 1

    result = client.request("station", "GET", "/stations")
    cache_control = result.headers.get("cache-control", "").lower()
    if "public" in cache_control or "immutable" in cache_control:
        raise CheckFailure(
            f"mutable station collection has unsafe Cache-Control: {cache_control!r}"
        )

    parsed = urllib.parse.urlsplit(client.url("station", "/stations"))
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    try:
        connection.request("GET", parsed.path, headers={"Authorization": f"Bearer {admin_token()}"})
        first = connection.getresponse()
        first.read()
        first_socket = connection.sock
        connection.request("GET", parsed.path, headers={"Authorization": f"Bearer {admin_token()}"})
        second = connection.getresponse()
        second.read()
        if (
            first.status != 200
            or second.status != 200
            or first_socket is None
            or connection.sock is None
        ):
            raise CheckFailure("persistent HTTP connection did not serve two successful requests")
        if connection.sock is not first_socket:
            raise CheckFailure("server closed an HTTP/1.1 connection after one request")
    finally:
        connection.close()
    return checks + 2


def create_case(client: APIClient, case: GraphCase) -> int:
    client.envelope("config", "POST", "/configs", case.config, http_status=201)
    client.envelope("station", "POST", "/stations", case.station_a, http_status=201)
    client.envelope("station", "POST", "/stations", case.station_b, http_status=201)
    client.envelope("train", "POST", "/trains", case.train)
    route_result = client.envelope("route", "POST", "/routes", case.route_input)
    assert_entity("route", route_result["data"], case.route, where="route create response")
    price_result = client.envelope("price", "POST", "/prices", case.price, http_status=201)
    assert_entity("price", price_result["data"], case.price, where="price create response")
    client.envelope("travel", "POST", "/trips", case.trip_input, http_status=201)
    return 7


def verify_case(client: APIClient, case: GraphCase, rng: random.Random) -> int:
    checks: list[Callable[[], None]] = []

    def check_config() -> None:
        data = client.envelope(
            "config", "GET", "/configs/" + urllib.parse.quote(case.config["name"], safe="")
        )["data"]
        assert_entity("config", data, case.config, where="config read-your-write")

    def check_station(item: Mapping[str, Any]) -> None:
        entities = index_entities("station", client.list_entities("station"))
        assert_entity("station", entities[item["id"]], item, where="station read-your-write")
        by_id = client.envelope("station", "GET", "/stations/name/" + item["id"])["data"]
        if by_id != item["name"]:
            raise CheckFailure(f"station name lookup returned {by_id!r}, expected {item['name']!r}")
        by_name = client.envelope(
            "station", "GET", "/stations/id/" + urllib.parse.quote(item["name"], safe="")
        )["data"]
        if by_name != item["id"]:
            raise CheckFailure(f"station ID lookup returned {by_name!r}, expected {item['id']!r}")

    def check_train() -> None:
        data = client.envelope("train", "GET", "/trains/" + case.train["id"])["data"]
        assert_entity("train", data, case.train, where="train read-your-write")

    def check_route() -> None:
        data = client.envelope("route", "GET", "/routes/" + case.route["id"])["data"]
        assert_entity("route", data, case.route, where="route read-your-write")
        found = client.envelope(
            "route",
            "GET",
            f"/routes/{case.route['startStationId']}/{case.route['terminalStationId']}",
        )["data"]
        if not isinstance(found, list) or case.route["id"] not in index_entities("route", found):
            raise CheckFailure("route start/terminal lookup omitted the newly written route")

    def check_price() -> None:
        data = client.envelope(
            "price",
            "GET",
            f"/prices/{case.price['routeId']}/{case.price['trainType']}",
        )["data"]
        assert_entity("price", data, case.price, where="price read-your-write")

    def check_trip() -> None:
        key = case.trip_input["tripId"]
        data = client.envelope("travel", "GET", "/trips/" + key)["data"]
        assert_entity("travel", data, case.trip, where="trip read-your-write")

    checks.extend(
        (
            check_config,
            lambda: check_station(case.station_a),
            lambda: check_station(case.station_b),
            check_train,
            check_route,
            check_price,
            check_trip,
        )
    )
    if case.retired_station_names:
        checks.append(lambda: verify_retired_station_names(client, case))
    rng.shuffle(checks)
    for check in checks:
        check()
    return len(checks)


def verify_retired_station_names(client: APIClient, case: GraphCase) -> None:
    for retired_name in case.retired_station_names:
        client.envelope(
            "station",
            "GET",
            "/stations/id/" + urllib.parse.quote(retired_name, safe=""),
            app_status=0,
        )


def update_case(client: APIClient, case: GraphCase, rng: random.Random) -> int:
    suffix = secrets.token_urlsafe(8)
    case.config["value"] = suffix
    case.config["description"] = "updated " + secrets.token_urlsafe(16)
    case.retired_station_names[case.station_a["name"]] = case.station_a["id"]
    case.retired_station_names[case.station_b["name"]] = case.station_b["id"]
    case.station_a["name"] = "Updated A " + suffix
    case.station_a["stayTime"] = rng.randint(41, 90)
    case.station_b["name"] = "Updated B " + suffix
    case.station_b["stayTime"] = rng.randint(41, 90)
    case.train["averageSpeed"] = rng.randint(351, 500)
    case.train["economyClass"] += 7
    case.route["distances"][1] += rng.randint(1, 99)
    case.route_input["distanceList"] = f"0,{case.route['distances'][1]}"
    case.price["basicPriceRate"] = round(rng.uniform(0.11, 0.89), 4)
    case.price["firstClassPriceRate"] = round(rng.uniform(0.91, 1.89), 4)
    case.trip_input["endTime"] += rng.randint(60_000, 3_600_000)
    case.trip["endTime"] = case.trip_input["endTime"]

    operations = [
        lambda: client.envelope("config", "PUT", "/configs", case.config),
        lambda: client.envelope("station", "PUT", "/stations", case.station_a),
        lambda: client.envelope("station", "PUT", "/stations", case.station_b),
        lambda: client.envelope("train", "PUT", "/trains", case.train),
        lambda: client.envelope("route", "POST", "/routes", case.route_input),
        lambda: client.envelope("price", "PUT", "/prices", case.price),
        lambda: client.envelope("travel", "PUT", "/trips", case.trip_input),
    ]
    rng.shuffle(operations)
    for operation in operations:
        operation()
    return len(operations)


def delete_case(client: APIClient, case: GraphCase, rng: random.Random) -> int:
    dependent = [
        lambda: client.envelope("travel", "DELETE", "/trips/" + case.trip_input["tripId"]),
        lambda: client.envelope("price", "DELETE", "/prices", case.price),
    ]
    rng.shuffle(dependent)
    for operation in dependent:
        operation()
    client.envelope("route", "DELETE", "/routes/" + case.route["id"])
    independent = [
        lambda: client.envelope("train", "DELETE", "/trains/" + case.train["id"]),
        lambda: client.envelope("station", "DELETE", "/stations", case.station_a),
        lambda: client.envelope("station", "DELETE", "/stations", case.station_b),
        lambda: client.envelope(
            "config", "DELETE", "/configs/" + urllib.parse.quote(case.config["name"], safe="")
        ),
    ]
    rng.shuffle(independent)
    for operation in independent:
        operation()
    return len(dependent) + 1 + len(independent)


def verify_deleted(client: APIClient, case: GraphCase) -> int:
    probes = [
        ("config", "/configs/" + urllib.parse.quote(case.config["name"], safe="")),
        ("train", "/trains/" + case.train["id"]),
        ("route", "/routes/" + case.route["id"]),
        ("price", f"/prices/{case.price['routeId']}/{case.price['trainType']}"),
        ("travel", "/trips/" + case.trip_input["tripId"]),
    ]
    for service, path in probes:
        client.envelope(service, "GET", path, app_status=0)
    stations = index_entities("station", client.list_entities("station"))
    station_checks = 0
    for station in (case.station_a, case.station_b):
        if station["id"] in stations:
            raise CheckFailure(f"deleted station {station['id']} remains visible")
        client.envelope("station", "GET", "/stations/name/" + station["id"], app_status=0)
        client.envelope(
            "station",
            "GET",
            "/stations/id/" + urllib.parse.quote(station["name"], safe=""),
            app_status=0,
        )
        station_checks += 3
    for retired_name in case.retired_station_names:
        client.envelope(
            "station",
            "GET",
            "/stations/id/" + urllib.parse.quote(retired_name, safe=""),
            app_status=0,
        )
        station_checks += 1
    return len(probes) + station_checks


def cleanup_cases(client: APIClient, cases: Sequence[GraphCase]) -> None:
    for case in reversed(cases):
        best_effort = [
            ("travel", "DELETE", "/trips/" + case.trip_input["tripId"], None),
            ("price", "DELETE", "/prices", case.price),
            ("route", "DELETE", "/routes/" + case.route["id"], None),
            ("train", "DELETE", "/trains/" + case.train["id"], None),
            ("station", "DELETE", "/stations", case.station_a),
            ("station", "DELETE", "/stations", case.station_b),
            (
                "config",
                "DELETE",
                "/configs/" + urllib.parse.quote(case.config["name"], safe=""),
                None,
            ),
        ]
        for service, method, path, body in best_effort:
            try:
                client.request(service, method, path, body)
            except Exception:
                pass


def wait_ready(client: APIClient, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error = "not attempted"
    while time.monotonic() < deadline:
        try:
            for service, (path, _) in WELCOME_PATHS.items():
                result = client.request(service, "GET", path)
                if result.status != 200:
                    raise RuntimeError(f"{service} returned HTTP {result.status}")
            return
        except Exception as exc:
            last_error = str(exc)
            time.sleep(0.1)
    raise CheckFailure(f"candidate/reference did not become ready: {last_error}")


class ManagedCandidate:
    def __init__(
        self,
        command: Sequence[str],
        cwd: Path,
        state_dir: Path,
        client: APIClient,
        startup_timeout: float,
    ) -> None:
        self._command = list(command)
        self._cwd = cwd
        self._state_dir = state_dir
        self._client = client
        self._startup_timeout = startup_timeout
        self._process: subprocess.Popen[bytes] | None = None
        self._log = tempfile.NamedTemporaryFile(prefix="train-ticket-candidate-", suffix=".log")

    def start(self) -> None:
        if self._process is not None:
            raise RuntimeError("candidate is already running")
        env = os.environ.copy()
        env["TRAIN_TICKET_DATA_DIR"] = str(self._state_dir)
        self._process = subprocess.Popen(
            self._command,
            cwd=self._cwd,
            env=env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            wait_ready(self._client, self._startup_timeout)
        except Exception as exc:
            self.stop(kill=True)
            self._log.seek(0)
            log = self._log.read().decode("utf-8", errors="replace")
            raise CheckFailure(f"candidate startup failed; log:\n{log[:4000]}") from exc

    def stop(self, *, kill: bool) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        process_group = process.pid
        requested_signal = signal.SIGKILL if kill else signal.SIGTERM
        signal_process_group(process_group, requested_signal)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        if not wait_process_group_exit(process_group, timeout=5):
            signal_process_group(process_group, signal.SIGKILL)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            if not wait_process_group_exit(process_group, timeout=5):
                raise CheckFailure(f"candidate process group {process_group} did not terminate")

    def crash_restart(self) -> None:
        self.stop(kill=True)
        self.start()

    def close(self) -> None:
        self.stop(kill=False)
        self._log.close()


def signal_process_group(process_group: int, requested_signal: signal.Signals) -> None:
    try:
        os.killpg(process_group, requested_signal)
    except ProcessLookupError:
        pass


def wait_process_group_exit(process_group: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def parse_targets(values: Sequence[str]) -> dict[str, str]:
    targets: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise argparse.ArgumentTypeError(f"target {value!r} must be NAME=URL")
        name, url = value.split("=", 1)
        if name not in SERVICE_PATHS:
            raise argparse.ArgumentTypeError(f"unknown target service {name!r}")
        targets[name] = url
    return targets


def parse_command(raw: str, name: str) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"{name} must be a JSON string array: {exc}") from exc
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise argparse.ArgumentTypeError(f"{name} must be a non-empty JSON string array")
    return value


def run_suite(
    client: APIClient,
    restart: Callable[[], None] | None,
    rng: random.Random,
    namespace: str,
    case_count: int,
) -> dict[str, Any]:
    checks = verify_welcomes_and_http(client)
    checks += verify_seed_catalog(client)
    cases = [make_case(rng, namespace, index) for index in range(case_count)]
    created: list[GraphCase] = []
    try:
        creation_order = list(cases)
        rng.shuffle(creation_order)
        for case in creation_order:
            checks += create_case(client, case)
            created.append(case)
            checks += verify_case(client, case, rng)

        update_order = list(cases)
        rng.shuffle(update_order)
        for case in update_order:
            checks += update_case(client, case, rng)
            checks += verify_case(client, case, rng)

        if restart is not None:
            restart()
            wait_ready(client, 30)
            persistence_order = list(cases)
            rng.shuffle(persistence_order)
            for case in persistence_order:
                checks += verify_case(client, case, rng)

        deletion_order = list(cases)
        rng.shuffle(deletion_order)
        for case in deletion_order:
            checks += delete_case(client, case, rng)
            checks += verify_deleted(client, case)
        created.clear()
    finally:
        cleanup_cases(client, created)
    return {
        "valid": True,
        "checks": checks,
        "random_cases": case_count,
        "namespace_hash": hashlib.sha256(namespace.encode("utf-8")).hexdigest(),
        "properties": {
            "exact_seed_catalog": True,
            "strict_entity_schemas": True,
            "read_your_write": True,
            "updates_visible": True,
            "deletes_visible": True,
            "cross_entity_graph": True,
            "crash_recovery": restart is not None,
            "persistent_http": True,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--target", action="append", default=[], metavar="NAME=URL")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--startup-timeout", type=float, default=15.0)
    parser.add_argument("--cases-min", type=int, default=2)
    parser.add_argument("--cases-max", type=int, default=5)
    parser.add_argument("--run-command-json")
    parser.add_argument("--candidate-dir", type=Path, default=Path.cwd())
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--restart-command-json")
    parser.add_argument("--output-json", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.cases_min < 1 or args.cases_max < args.cases_min:
        raise SystemExit("invalid randomized case bounds")
    if args.run_command_json and args.restart_command_json:
        raise SystemExit("provide at most one of --run-command-json or --restart-command-json")

    targets = parse_targets(args.target)
    client = APIClient(args.base_url, targets, args.timeout)
    seed = secrets.randbits(128)
    rng = random.Random(seed)
    namespace = "vs" + secrets.token_hex(12)
    case_count = rng.randint(args.cases_min, args.cases_max)
    managed: ManagedCandidate | None = None
    temporary_state: tempfile.TemporaryDirectory[str] | None = None
    try:
        if args.run_command_json:
            command = parse_command(args.run_command_json, "--run-command-json")
            if args.state_dir is None:
                temporary_state = tempfile.TemporaryDirectory(prefix="train-ticket-state-")
                state_dir = Path(temporary_state.name)
            else:
                state_dir = args.state_dir.resolve()
                state_dir.mkdir(parents=True, exist_ok=True)
            managed = ManagedCandidate(
                command,
                args.candidate_dir.resolve(),
                state_dir,
                client,
                args.startup_timeout,
            )
            managed.start()
            restart = managed.crash_restart
        elif args.restart_command_json:
            restart_command = parse_command(args.restart_command_json, "--restart-command-json")

            def restart() -> None:
                subprocess.run(restart_command, check=True)

            wait_ready(client, args.startup_timeout)
        else:
            restart = None
            wait_ready(client, args.startup_timeout)

        result = run_suite(client, restart, rng, namespace, case_count)
        result["seed_sha256"] = hashlib.sha256(str(seed).encode("ascii")).hexdigest()
        if args.output_json:
            args.output_json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:
        failure = {
            "valid": False,
            "error": str(exc),
            "seed_sha256": hashlib.sha256(str(seed).encode("ascii")).hexdigest(),
        }
        if args.output_json:
            args.output_json.write_text(json.dumps(failure, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(failure, indent=2), file=sys.stderr)
        return 1
    finally:
        if managed is not None:
            managed.close()
        if temporary_state is not None:
            temporary_state.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
