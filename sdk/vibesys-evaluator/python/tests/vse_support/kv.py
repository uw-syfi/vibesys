"""A tiny key-value service and a task for it (the kv-store example's, scaled down).

``KvService.handle`` is the whole application: the deterministic tests call it
through the Fake loadgen, the end-to-end test serves it over HTTP. Its knobs
inject faults: errors a phase counts, writes an accuracy check would catch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from urllib.parse import parse_qsl

import vibesys_evaluator as vse
from vibesys_evaluator import Rand
from vibesys_evaluator import microservices as ms
from vibesys_evaluator.microservices import (
    Operation,
    Request,
    Step,
    Template,
    Workload,
    get,
    http,
    k8s,
    post,
    put,
    ref,
)

SEED = 11
KEYS = 64
USERS = 4


@dataclass
class KvService:
    """register/login/kv over HTTP semantics, in memory.

    Attributes:
        lose_writes: Acknowledge a PUT without storing it (reads then see old values).
        fail_every: Answer every n-th request with 500 (0: never).
    """

    lose_writes: bool = False
    fail_every: int = 0
    users: dict[str, str] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)
    values: dict[str, bytes] = field(default_factory=dict)
    requests: int = 0

    def handle(
        self, method: str, target: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, bytes]:
        """Answer one request; ``target`` is the path and query."""
        self.requests += 1
        if self.fail_every and self.requests % self.fail_every == 0:
            return 500, b"injected failure"
        path, _, _query = target.partition("?")
        if path.startswith("/kv/"):
            return self._kv(method, path.removeprefix("/kv/"), headers, body)
        route = {
            ("POST", "/register"): self._register,
            ("POST", "/login"): self._login,
            ("POST", "/form"): self._form,
        }.get((method, path))
        return route(body) if route else (404, b"not found")

    def _register(self, body: bytes) -> tuple[int, bytes]:
        account = json.loads(body)
        self.users[account["user"]] = account["password"]
        return 200, b"{}"

    def _login(self, body: bytes) -> tuple[int, bytes]:
        account = json.loads(body)
        if self.users.get(account["user"]) != account["password"]:
            return 401, b'{"error":"wrong password"}'
        token = f"t-{account['user']}-{self.requests}"
        self.tokens[token] = account["user"]
        return 200, json.dumps({"token": token}).encode()

    @staticmethod
    def _form(body: bytes) -> tuple[int, bytes]:
        return 200, json.dumps(dict(parse_qsl(body.decode()))).encode()

    def _kv(self, method: str, key: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        if headers.get("authorization", "").removeprefix("Bearer ") not in self.tokens:
            return 403, b"no token"
        if method == "GET":
            return (200, self.values[key]) if key in self.values else (404, b"")
        if method == "PUT":
            if not self.lose_writes:
                self.values[key] = body
            return 200, b""
        if method == "DELETE":
            if key not in self.values:
                return 404, b""
            if not self.lose_writes:
                del self.values[key]
            return 200, b""
        return 404, b"not found"


# ------------------------------------------------------------------ the task


class Data:
    """USERS accounts and KEYS // 2 seeded keys, from SEED."""

    def __init__(self) -> None:
        self.users = [
            (f"u{n}", f"pw-{Rand(SEED, 'user', n).below(1 << 40):x}") for n in range(USERS)
        ]
        self.values = [value(Rand(SEED, "key", k)) for k in range(KEYS // 2)]


def value(rand: Rand) -> bytes:
    return bytes(97 + rand.below(26) for _ in range(4 + rand.below(29)))


def seed_service(service: KvService, data: Data) -> None:
    """What the task's seed does, straight into the service."""
    for user, password in data.users:
        service.users[user] = password
    for key, body in enumerate(data.values):
        service.values[f"k{key}"] = body


class Model:
    def __init__(self, data: Data, rand: Rand) -> None:
        self.data = data
        self.draws = rand.below(1000)
        self.phase = 0
        self.written: dict[int, int] = {}


def begin_phase(m: Model, phase: int) -> None:
    m.phase = phase


AUTH: Template = "Bearer " + ref("token")


def login(m: Model, rand: Rand) -> Step:
    user, password = m.data.users[rand.below(USERS)]
    return post("/login", json={"user": user, "password": password}, capture={"token": "/token"})


def read(m: Model, rand: Rand) -> Request:
    key = rand.zipf(KEYS, 0.99)
    return Request(
        login(m, rand),
        get(f"/kv/k{key}", headers={"Authorization": AUTH}, expect=(200, 404)),
        info={"key": key},
    )


def write(m: Model, rand: Rand) -> Request:
    key, seed = rand.zipf(KEYS, 0.99), rand.below(1 << 62)
    m.written[key] = m.written.get(key, 0) + 1  # build may change the model
    return Request(
        login(m, rand),
        put(f"/kv/k{key}", body=value(Rand(seed)), headers={"Authorization": AUTH}),
        info={"key": key, "seed": seed, "phase": m.phase, "nth": m.written[key]},
    )


def delete(m: Model, rand: Rand) -> Request:
    key = rand.zipf(KEYS, 0.99)
    return Request(
        login(m, rand),
        http("DELETE", f"/kv/k{key}", headers={"Authorization": AUTH}, expect=(200, 404)),
        info={"key": key},
    )


WORKLOAD = Workload(
    model=Model,
    operations=[
        Operation(name="read", weight=75, build=read),
        Operation(name="write", weight=20, build=write),
        Operation(name="delete", weight=5, build=delete),
    ],
    begin_phase=begin_phase,
)

SYSTEM = k8s.App(
    name="kv-test",
    manifest="k8s/kv.yaml",
    images={"KV_IMAGE": "server"},
    gateway="http://kv:8080",
    ready=[post("/login", json={"user": "u0", "password": "wrong"}, expect=401)],
)

METRICS = {
    "operations_per_second": vse.Metric(unit="ops/s", direction="max"),
    "latency_ms.p99": vse.Metric(unit="ms", direction="min"),
}


def benchmark(run: ms.Run) -> None:
    """A closed loop, then an open loop at half its throughput (both short)."""
    closed = run.closed_loop(clients=4, secs=1, warmup=0.5)
    opened = run.open_loop(rate=0.5 * closed.ops_per_sec, secs=1, warmup=0.5, arrivals="uniform")
    run.report({"operations_per_second": closed.ops_per_sec, "latency_ms.p99": opened.p99_ms})
