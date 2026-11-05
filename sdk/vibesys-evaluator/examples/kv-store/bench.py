"""kv-store: a small HTTP key-value service (spec example, a third style).

Written against the intended vibesys-evaluator API; it does not run yet. It
differs from the two DeathStarBench tasks on purpose: one Deployment and no
data tier; the dataset is a few lines here and is seeded through the
service's own API; every operation logs in and uses the token it got (a
template the loadgen fills from the first response); reads must see every
acknowledged write at once (bound 0); the score is latency at a fixed
Poisson-arrival rate, with no closed loop. bench_go.py is the same task with
this workload written in Go (generator/): both write the same request stream.

The service (candidate directory `server/`, its own Dockerfile) offers
    POST /register {"user", "password"}
    POST /login {"user", "password"} -> {"token"}
    GET  /kv/<key>   (Authorization: Bearer <token>) -> the value, or 404
    PUT  /kv/<key>   (Authorization: Bearer <token>) the value as the body
"""

from __future__ import annotations

import json
import urllib.request

import vibesys_evaluator as vse
from vibesys_evaluator import microservices as ms
from vibesys_evaluator import Rand
from vibesys_evaluator.microservices import Call, Versions
from vibesys_evaluator.microservices import k8s
from vibesys_evaluator.microservices import Operation, Request, Step, Template, Workload, get, post, put, ref

SEED = 7
KEYS = 100_000
USERS = 100
RATE = 2_000  # operations/s offered


# ------------------------------------------------------- (2) initial data

class Data:
    """KEYS keys of 16-512 bytes and USERS accounts, from SEED."""

    def __init__(self) -> None:
        self.users = [(f"u{n}", f"pw-{Rand(SEED, 'user', n).below(1 << 40):x}") for n in range(USERS)]
        self.values = [value(Rand(SEED, "key", k)) for k in range(KEYS // 2)]  # half the keys exist


def value(rand: Rand) -> bytes:
    return bytes(97 + rand.below(26) for _ in range(16 + rand.below(497)))


def seed(data: Data) -> None:
    """Load the accounts and keys through the API (in-cluster Job): no data tier to write."""
    for user, password in data.users:
        account = json.dumps({"user": user, "password": password}).encode()
        urllib.request.urlopen(urllib.request.Request(
            "http://kv:8080/register", method="POST", data=account)).close()
    user, password = data.users[0]
    login = urllib.request.Request("http://kv:8080/login", method="POST",
                                   data=json.dumps({"user": user, "password": password}).encode())
    with urllib.request.urlopen(login) as response:
        token = json.load(response)["token"]
    for key, body in enumerate(data.values):
        request = urllib.request.Request(f"http://kv:8080/kv/k{key}", data=body, method="PUT",
                                         headers={"Authorization": f"Bearer {token}"})
        urllib.request.urlopen(request).close()


# -------------------------------------------------- (1) system under test

SYSTEM = k8s.App(
    name="kv-store",
    manifest="k8s/kv.yaml",
    images={"KV_IMAGE": "server"},
    seed=seed,  # no data_tier: the whole manifest is applied and ready before the seed
    gateway="http://kv:8080",
    ready=[post("/login", json={"user": "u0", "password": "wrong"}, expect=401)],
)


# ------------------------------------------------------------- (3) workload

class Model:
    def __init__(self, data: Data, rand: Rand) -> None:
        self.data = data
        self.values: Versions[int, bytes | None] = Versions(bound_secs=0.0)

    def seeded(self, key: int) -> bytes | None:
        return self.data.values[key] if key < len(self.data.values) else None


AUTH: Template = "Bearer " + ref("token")  # filled from the login step's response


def login(m: Model, rand: Rand) -> Step:
    """Login, then use: this first step captures the token the second one sends (AUTH)."""
    user, password = m.data.users[rand.below(USERS)]
    return post("/login", json={"user": user, "password": password}, capture={"token": "/token"})


def read(m: Model, rand: Rand) -> Request:
    key = rand.zipf(KEYS, 0.99)
    return Request(login(m, rand),
                   get(f"/kv/k{key}", headers={"Authorization": AUTH}, expect=(200, 404)),
                   info={"key": key})


def write(m: Model, rand: Rand) -> Request:
    key, seed_ = rand.zipf(KEYS, 0.99), rand.below(1 << 62)
    return Request(login(m, rand),
                   put(f"/kv/k{key}", body=value(Rand(seed_)), headers={"Authorization": AUTH}),
                   info={"key": key, "seed": seed_}, exclusive=f"k{key}")


def apply_write(m: Model, call: Call) -> None:
    m.values.write(call.info["key"], value(Rand(call.info["seed"])), call)


def check_read(m: Model, call: Call) -> str | None:
    assert call.start is not None and call.end is not None
    key = call.info["key"]
    response: ms.Response = call.responses[1]
    got = response.body if response.status == 200 else None
    allowed = m.values.allowed(key, m.seeded(key), start=call.start, end=call.end)
    return None if got in allowed else f"k{key}: {got!r:.40}, allowed {len(allowed)} values"


def check_write(m: Model, call: Call) -> str | None:
    return None if call.responses[1].status == 200 else f"PUT: HTTP {call.responses[1].status}"


WORKLOAD = Workload(
    model=Model,
    operations=[
        # A quarter of the reads are checked: the bodies of the others are kept as hash and length.
        Operation(name="read", weight=90, build=read, check=check_read, sample=0.25),
        Operation(name="write", weight=10, build=write, check=check_write, apply=apply_write),
    ],
)


# ------------------------------------------------------------ (4) benchmark

def benchmark(run: ms.Run) -> None:
    phase: ms.PhaseResult = run.open_loop(rate=RATE, secs=60, warmup=15)  # Poisson arrivals
    run.report({"latency_ms.p50": phase.p50_ms, "latency_ms.p99": phase.p99_ms,
                "operations_per_second": phase.ops_per_sec})


# ------------------------------------------------------------- (5) validity

if __name__ == "__main__":
    ms.main(
        dataset=Data,
        system=SYSTEM,
        workload=WORKLOAD,
        benchmark=benchmark,
        metrics={
            "latency_ms.p50": vse.Metric(unit="ms", direction="min"),
            "latency_ms.p99": vse.Metric(unit="ms", direction="min"),
            "operations_per_second": vse.Metric(unit="ops/s", direction="max"),
        },
        validity=vse.Validity(max_error_rate=0.001, min_sustained_ratio=0.99),
    )
