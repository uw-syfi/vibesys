"""hotel-k8s: DeathStarBench Hotel Reservation on Kubernetes (spec example).

Written against the intended vibesys-evaluator API (api/public-api.txt); it
does not run yet. It replaces the task's run.py (benchmark path) and the Go
benchmark adapter (evaluator/internal/apps/hotel, cmd/loadgen, cmd/seed). The
mix, distributions, phases, the 0.7x open-loop rate and the validity rules are
those of workload/closed.toml and workload/open.toml.

    python3 /task/bench.py benchmark --project-root ROOT --vs-output PATH

The five parts: (1) the system under test, (2) the initial data (seed),
(3) the workload (operations and the model they are checked against),
(4) the benchmark function, (5) the validity rules.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, timedelta

import dataset as ds
import vibesys_evaluator as vse
from vibesys_evaluator import microservices as ms
from vibesys_evaluator import Rand
from vibesys_evaluator.microservices import Call, Versions
from vibesys_evaluator.microservices import k8s
from vibesys_evaluator.microservices import Operation, Request, Workload, get, post

# ---------------------------------------------------------------- (2) seed


def seed(data: ds.Dataset) -> None:
    """Write the dataset into the six MongoDB servers (runs as an in-cluster Job).

    Runs after the data tier is Available and before the services start: each
    service seeds its own test data into an empty collection, and geo and
    recommendation load their collection once at startup.
    """
    import pymongo  # in the task image (Dockerfile: pip install pymongo)

    servers = {"geo-db": "mongodb-geo:27018", "profile-db": "mongodb-profile:27019",
               "rate-db": "mongodb-rate:27020", "recommendation-db": "mongodb-recommendation:27021",
               "reservation-db": "mongodb-reservation:27022", "user-db": "mongodb-user:27023"}
    for database, collections in ds.documents(data).items():
        client = pymongo.MongoClient(f"mongodb://{servers[database]}")
        for name, documents in collections.items():
            client[database][name].insert_many(documents, ordered=False)
        # Indexes on the services' query keys (internal/seed/documents.go).


# -------------------------------------------------- (1) system under test

PROBE_HOTEL = {"inDate": "2300-01-01", "outDate": "2300-01-02", "lat": "37.7867", "lon": "-122.4112"}

SYSTEM = k8s.App(
    name="hotel-k8s",  # namespaces hotel-k8s-<hex>, labels, and the sweep of stale ones
    manifest="k8s/manifest.yaml",  # ${NAMESPACE} and ${HOTEL_IMAGE} are substituted
    images={"HOTEL_IMAGE": "hotelReservation"},  # built from PROJECT_ROOT/hotelReservation
    data_tier="tier=data",  # MongoDB and memcached: applied and Available before the seed
    seed=seed,
    gateway="http://frontend:5000",
    # Readiness: every Deployment has all replicas available, then each probe
    # answers its expected status on fresh connections, twice per gateway replica.
    ready=[
        get("/user", query={"username": "Cornell_30", "password": "0000000000"}),
        get("/recommendations", query={"require": "rate", "lat": "37.7867", "lon": "-122.4112"}),
        get("/hotels", query=PROBE_HOTEL),
    ],
    # The accuracy oracle's crash hook (`bench.py ctl stop|start`) scales these
    # to 0 and back; MongoDB stays up, so its emptyDir keeps the data.
    restartable=["frontend", "geo", "profile", "rate", "recommendation", "reservation", "search",
                 "user", "memcached-reserve"],
)


# --------------------------------------------------------------- (3) model

STALENESS_SECS = 1.0  # the task's visibility contract (OBJECTIVE.md)
RESERVATION_BLOCK_NIGHTS = 512 * 17  # far-future nights a sample may use
RESERVATIONS_PER_NIGHT = 128


@dataclass(frozen=True)
class Prices:
    bookable: float
    total: float
    inclusive: float


class Model:
    """The seeded data plus this sample's writes, with their times.

    One instance per phase pod: the library builds it with Model(data, rand)
    (rand is the sample's stream, the same in every phase), replays the
    sample's earlier writes into it through each operation's `apply`, then
    either generates the phase's requests (writes applied as pending as they
    are generated) or, after the phase, checks a sample of the responses.
    This replaces the Go adapter's Reset: the next phase knows every value the
    previous ones wrote, so nothing is restored.
    """

    def __init__(self, data: ds.Dataset, rand: Rand) -> None:
        self.data = data
        self.ref = ds.Reference(data)
        self.profiles: Versions[str, tuple[str, str]] = Versions(bound_secs=STALENESS_SECS)
        self.prices: Versions[tuple[str, str], Prices] = Versions(bound_secs=STALENESS_SECS)
        self.rooms: Versions[str, int] = Versions(bound_secs=STALENESS_SECS)
        # Added hotels: key -> the hotel once written (None before).
        self.additions: Versions[str, ds.Hotel | None] = Versions(bound_secs=STALENESS_SECS)
        self.added: list[ds.Hotel] = []  # every addition sent, in plan order
        self.confirmed: list[ds.Hotel] = []  # additions acknowledged in earlier phases
        # Reservations: a sample-owned block of far-future nights, outside the
        # rate window searches use; 128 distinct hotels per night.
        self.first_night = date(3000, 1, 1) + timedelta(rand.below(146097 * 17 - RESERVATION_BLOCK_NIGHTS))
        self.next_slot = 0
        self.used: dict[int, set[int]] = {}
        self.cell = rand.below(ds.CELL_COUNT)  # where this sample's additions go
        self.tag = rand.below(1 << 48)  # added-hotel IDs a<tag>-<n> never collide


# --------------------------------------------------- (3) request distributions

STAY_NIGHTS = (30, 24, 16, 11, 8, 6, 5)  # relative weight of a stay of n+1 nights
CELL_SHARE = 0.03  # searches and distance recommendations near an addition


def location(m: Model, rand: Rand) -> tuple[str, str]:
    """A city by Zipf rank (s = 1), then normal offsets of the city's spread; 5 decimals."""
    city = m.data.cities[rand.zipf(len(m.data.cities), 1.0)]
    lat = city.template.lat + city.spread_km * rand.normal() / ds.KM_PER_DEGREE_LAT
    lon = city.template.lon + city.spread_km * rand.normal() / city.km_per_degree_lon
    return f"{lat:.5f}", f"{lon:.5f}"


def stay(rand: Rand) -> tuple[str, str]:
    nights = 1 + rand.weighted(STAY_NIGHTS)
    start = ds.RATE_WINDOW_START + timedelta(rand.below(ds.RATE_WINDOW_DAYS - nights + 1))
    return str(start), str(start + timedelta(nights))


def popular_hotel(m: Model, rand: Rand) -> ds.Hotel:
    """A hotel by popularity: a city by Zipf rank (s = 1), a hotel by Zipf rank (s = 0.75)."""
    city = m.data.cities[rand.zipf(len(m.data.cities), 1.0)]
    return m.data.hotels[city.hotels[rand.zipf(len(city.hotels), 0.75)]]


def user(m: Model, rand: Rand) -> ds.User:
    return m.data.users[rand.zipf(len(m.data.users), 0.75)]


def capacity_managed(hotel: ds.Hotel) -> bool:
    """A quarter of the hotels, fixed by ID: set_capacity's, never reserved."""
    return Rand(int(hotel.id), "capacity").below(4) == 0


def near_addition(m: Model, rand: Rand) -> tuple[str, str] | None:
    """3% of the time, a point near an addition acknowledged in an earlier phase."""
    if rand.random() >= CELL_SHARE or not m.confirmed:
        return None
    anchor = m.confirmed[rand.below(len(m.confirmed))]
    return f"{anchor.lat + (2 * rand.random() - 1) * 0.5 / ds.KM_PER_DEGREE_LAT:.5f}", \
           f"{anchor.lon + (2 * rand.random() - 1) * 0.5 / 70:.5f}"


# ---------------------------------------------------------- (3) operations
# build(model, rand) -> Request. `info` holds what the request was built from;
# it is not sent, and comes back in the Call given to check and apply.

def search(m: Model, rand: Rand) -> Request:
    in_date, out_date = stay(rand)
    near = near_addition(m, rand)
    if near is not None:
        query = {"inDate": in_date, "outDate": out_date, "lat": near[0], "lon": near[1]}
        return Request(get("/hotels", query=query), info={"near": list(near)})
    for attempt in range(8):  # redraw where geo's own ranking ties
        lat, lon = location(m, rand.child("attempt", attempt))
        expected, exact = m.ref.search_candidates(lat, lon)
        if exact:
            query = {"inDate": in_date, "outDate": out_date, "lat": lat, "lon": lon}
            if rand.random() < 0.5:
                query["locale"] = "en"
            return Request(get("/hotels", query=query), info={"expected": expected})
    raise ValueError("no unambiguous search location in 8 draws")


def recommend(require: str):  # one family, three operations
    def build(m: Model, rand: Rand) -> Request:
        near = near_addition(m, rand) if require == "dis" else None
        lat, lon = near or location(m, rand)
        query = {"require": require, "lat": lat, "lon": lon}
        if near is None and rand.random() < 0.5:
            query["locale"] = "en"
        return Request(get("/recommendations", query=query), info={"lat": lat, "lon": lon})
    return build


def login(valid: bool):
    def build(m: Model, rand: Rand) -> Request:
        u = user(m, rand)
        password = u.password if valid else u.password + "-invalid"
        return Request(get("/user", query={"username": u.username, "password": password}))
    return build


def reserve_capacity(m: Model, rand: Rand) -> Request:
    """Reserve a hotel's full capacity on a sample-owned night, then one room more.

    Two steps: the second must be rejected. `exclusive` serializes every
    reservation (the service's capacity check is not transactional), as the
    Go adapter's mutex did.
    """
    slot = m.next_slot
    night = slot // RESERVATIONS_PER_NIGHT
    used = m.used.get(night, set())
    hotel = next((h for h in (popular_hotel(m, rand.child("hotel", a)) for a in range(16))
                  if int(h.id) - 1 not in used and not capacity_managed(h)), None)
    if hotel is None:  # the night's popular hotels are taken: the city's next ones
        city = m.data.cities[popular_hotel(m, rand).city]
        hotel = next(m.data.hotels[i] for i in city.hotels
                     if i not in used and not capacity_managed(m.data.hotels[i]))
    u = user(m, rand.child("reserve-user"))
    in_date = m.first_night + timedelta(night)
    query = {"inDate": str(in_date), "outDate": str(in_date + timedelta(1)), "hotelId": hotel.id,
             "customerName": f"vibesys-{rand.below(1 << 63):016x}", "username": u.username,
             "password": u.password}
    capacity = m.rooms.latest(hotel.id, hotel.capacity)
    return Request(
        get("/reservation", query={**query, "number": str(capacity)}),
        get("/reservation", query={**query, "number": "1"}),
        info={"slot": slot, "hotel": int(hotel.id) - 1},
        exclusive="reservation",
    )


def update_rate(m: Model, rand: Rand) -> Request:
    hotel = popular_hotel(m, rand)
    plan = hotel.rate_plans[rand.below(len(hotel.rate_plans))]
    total = round(plan.total_rate * (0.8 + 0.45 * rand.random()), 2)
    prices = Prices(total, total, round(total * plan.total_rate_inclusive / plan.total_rate, 2))
    query = {"hotelId": hotel.id, "code": plan.code, "bookableRate": str(prices.bookable),
             "totalRate": str(prices.total), "totalRateInclusive": str(prices.inclusive)}
    # Writes to one item are serialized by `exclusive`, so its versions are in
    # application order (the Go adapter redrew another item instead).
    return Request(post("/admin/rate", query=query), exclusive=f"rate:{hotel.id}:{plan.code}",
                   info={"hotel": hotel.id, "code": plan.code, "prices": [prices.bookable, prices.total,
                                                                         prices.inclusive]})


def update_profile(m: Model, rand: Rand) -> Request:
    hotel = popular_hotel(m, rand)
    name, phone = m.profiles.latest(hotel.id, (hotel.name, hotel.phone))
    phone = ds.new_phone(hotel, rand.child("phone"))
    query = {"hotelId": hotel.id, "phone": phone}
    if rand.random() < 0.1:
        name = query["name"] = ds.new_name(hotel, rand.child("name"))
    return Request(post("/admin/profile", query=query), exclusive=f"profile:{hotel.id}",
                   info={"hotel": hotel.id, "name": name, "phone": phone})


def set_capacity(m: Model, rand: Rand) -> Request:
    """Change a capacity-managed hotel's rooms, then reserve one room more: rejected at once."""
    hotel = next(h for h in (popular_hotel(m, rand.child("hotel", a)) for a in range(64))
                 if capacity_managed(h))
    rooms = max(1, hotel.capacity // 2) + rand.below(2 * hotel.capacity - max(1, hotel.capacity // 2) + 1)
    u = user(m, rand.child("reserve-user"))
    probe = {"inDate": str(m.first_night), "outDate": str(m.first_night + timedelta(1)),
             "hotelId": hotel.id, "customerName": f"vibesys-capacity-{rand.below(1 << 63):016x}",
             "username": u.username, "password": u.password, "number": str(rooms + 1)}
    return Request(
        post("/admin/capacity", query={"hotelId": hotel.id, "rooms": str(rooms)}),
        get("/reservation", query=probe),
        exclusive=f"capacity:{hotel.id}",
        info={"hotel": hotel.id, "rooms": rooms},
    )


def add_hotel(m: Model, rand: Rand) -> Request:
    n = len(m.added) + 1
    lat, lon = ds.cell_point(m.cell, rand.child("point"))
    hotel = ds.added_hotel(f"a{m.tag:x}-{n}", rand.child("hotel"), lat, lon)
    # Keep the "rate" and "price" recommendations of the whole namespace.
    rating = min(hotel.rating, round(m.ref.max_rating - 0.01, 2))
    price = max(hotel.price, round(m.ref.min_price + 0.01, 2))
    body = {"hotelId": hotel.id, "name": hotel.name, "phoneNumber": hotel.phone, "lat": lat,
            "lon": lon, "rating": rating, "price": price, "rooms": hotel.capacity,
            "ratePlans": [{"code": p.code, "inDate": p.in_date, "outDate": p.out_date,
                           "roomType": {"code": p.room_code, "bookableRate": p.bookable_rate,
                                        "totalRate": p.total_rate,
                                        "totalRateInclusive": p.total_rate_inclusive}}
                          for p in hotel.rate_plans]}
    return Request(post("/admin/hotel", json=body), info={"n": n, "seed": rand.below(1 << 62)})


def rates(m: Model, rand: Rand) -> Request:
    hotel = popular_hotel(m, rand)
    return Request(get("/rates", query={"hotelId": hotel.id}), info={"hotel": hotel.id})


# ------------------------------------------------- (3) apply: writes -> model
# Called for every write: as pending (call.ok is None, no times) while the
# phase is generated, and with its outcome and times when the sample's history
# is replayed. A failed write counts as possibly applied (Versions does that).

def apply_rate(m: Model, call: Call) -> None:
    b, t, i = call.info["prices"]
    m.prices.write((call.info["hotel"], call.info["code"]), Prices(b, t, i), call)


def apply_profile(m: Model, call: Call) -> None:
    m.profiles.write(call.info["hotel"], (call.info["name"], call.info["phone"]), call)


def apply_capacity(m: Model, call: Call) -> None:
    m.rooms.write(call.info["hotel"], call.info["rooms"], call)


def apply_reservation(m: Model, call: Call) -> None:
    slot = call.info["slot"]
    m.next_slot = max(m.next_slot, slot + 1)
    m.used.setdefault(slot // RESERVATIONS_PER_NIGHT, set()).add(call.info["hotel"])


def apply_addition(m: Model, call: Call) -> None:
    n = call.info["n"]
    lat, lon = ds.cell_point(m.cell, Rand(call.info["seed"]))
    hotel = ds.added_hotel(f"a{m.tag:x}-{n}", Rand(call.info["seed"]), lat, lon)
    m.added.append(hotel)
    m.additions.write(hotel.id, hotel, call)
    if call.ok:
        m.confirmed.append(hotel)


# ------------------------------------------------------ (3) check: responses
# check(model, call) -> None, or what is wrong. Called after the phase, in
# order of start time, for every write and a sample of the reads (10% unless
# Operation.sample says otherwise). start and end bound the read's window.

LOGIN_FAILURE = "Failed. Please check your username and password. "
RESERVATION_FAILURE = "Failed. Already reserved. "


def message(call: Call, step: int, expected: str) -> str | None:
    value = call.responses[step].json()
    if value != {"message": expected}:
        return f"step {step}: {value!r}, want {{'message': {expected!r}}}"
    return None


def check_features(m: Model, call: Call, accept: list[list[str]]) -> str | None:
    """Strict GeoJSON: feature IDs one of `accept`, profiles as written (float32 coordinates)."""
    assert call.start is not None and call.end is not None
    features = call.responses[0].json().get("features", [])
    ids = sorted(f["id"] for f in features)
    if all(ids != sorted(a) for a in accept):
        return f"hotels {ids}, want one of {accept}"
    for feature in features:
        hotel = m.data.by_id.get(feature["id"]) or next(h for h in m.added if h.id == feature["id"])
        allowed = m.profiles.allowed(hotel.id, (hotel.name, hotel.phone), start=call.start, end=call.end)
        got = (feature["properties"]["name"], feature["properties"]["phone_number"])
        if got not in allowed:
            return f"hotel {hotel.id} profile {got}, allowed {allowed}"
    return None


def additions_at(m: Model, call: Call) -> tuple[tuple[ds.Hotel, ...], tuple[ds.Hotel, ...]]:
    """(required, optional) additions for a read in [call.start, call.end]."""
    assert call.start is not None and call.end is not None
    required, optional = [], []
    for hotel in m.added:
        allowed = m.additions.allowed(hotel.id, None, start=call.start, end=call.end)
        if allowed == (hotel,):
            required.append(hotel)
        elif hotel in allowed:
            optional.append(hotel)
    return tuple(required), tuple(optional[:8])


def check_search(m: Model, call: Call) -> str | None:
    if "expected" in call.info:
        return check_features(m, call, [call.info["expected"]])
    # Near an addition: the answer for the required additions plus some
    # subset of the optional ones (at most 8 of them).
    lat, lon = call.info["near"]
    required, optional = additions_at(m, call)
    accept = [m.ref.search_candidates(lat, lon, required + extra)[0] for extra in subsets(optional)]
    return check_features(m, call, accept)


def check_recommendation(require: str):
    def check(m: Model, call: Call) -> str | None:
        if require == "rate":
            return check_features(m, call, [m.ref.best_rated()])
        if require == "price":
            return check_features(m, call, [m.ref.cheapest()])
        lat, lon = float(call.info["lat"]), float(call.info["lon"])
        required, optional = additions_at(m, call)
        return check_features(m, call, [m.ref.nearest(lat, lon, required + extra)
                                        for extra in subsets(optional)])
    return check


def check_login(valid: bool):
    def check(m: Model, call: Call) -> str | None:
        return message(call, 0, "Login successfully!" if valid else LOGIN_FAILURE)
    return check


def check_reservation(m: Model, call: Call) -> str | None:
    return message(call, 0, "Reserve successfully!") or message(call, 1, RESERVATION_FAILURE)


def check_write(expected: str):
    def check(m: Model, call: Call) -> str | None:
        return message(call, 0, expected)
    return check


def check_capacity(m: Model, call: Call) -> str | None:
    return message(call, 0, "Capacity updated.") or message(call, 1, RESERVATION_FAILURE)


def check_rates(m: Model, call: Call) -> str | None:
    assert call.start is not None and call.end is not None
    hotel = m.data.by_id[call.info["hotel"]]
    body = call.responses[0].json()
    if body.get("hotelId") != hotel.id or len(body.get("ratePlans", [])) != len(hotel.rate_plans):
        return f"rates of {body.get('hotelId')}: {len(body.get('ratePlans', []))} plans"
    for plan, got in zip(sorted(hotel.rate_plans, key=lambda p: p.code),
                         sorted(body["ratePlans"], key=lambda p: p["code"])):
        room = got["roomType"]
        prices = Prices(room["bookableRate"], room["totalRate"], room["totalRateInclusive"])
        seeded = Prices(plan.bookable_rate, plan.total_rate, plan.total_rate_inclusive)
        allowed = m.prices.allowed((hotel.id, plan.code), seeded, start=call.start, end=call.end)
        if prices not in allowed:
            return f"plan {plan.code} of hotel {hotel.id}: {prices}, allowed {allowed}"
    return None


def subsets(items: tuple[ds.Hotel, ...]) -> list[tuple[ds.Hotel, ...]]:
    out: list[tuple[ds.Hotel, ...]] = [()]
    for item in items:
        out += [s + (item,) for s in out]
    return out


# ------------------------------------------------------------- (3) workload

WORKLOAD = Workload(
    model=Model,
    operations=[
        Operation(name="search_hotels", weight=6000, build=search, check=check_search),
        Operation(name="recommend_distance", weight=1300, build=recommend("dis"),
                  check=check_recommendation("dis")),
        Operation(name="recommend_rate", weight=1300, build=recommend("rate"),
                  check=check_recommendation("rate")),
        Operation(name="recommend_price", weight=1300, build=recommend("price"),
                  check=check_recommendation("price")),
        Operation(name="login_valid", weight=30, build=login(True), check=check_login(True)),
        Operation(name="login_invalid", weight=20, build=login(False), check=check_login(False)),
        Operation(name="hotel_rates", weight=400, build=rates, check=check_rates),
        # Writes: `apply` makes them writes, checked at 100%.
        Operation(name="reserve_capacity", weight=50, build=reserve_capacity,
                  check=check_reservation, apply=apply_reservation),
        Operation(name="update_rate", weight=42, build=update_rate,
                  check=check_write("Rate updated."), apply=apply_rate),
        Operation(name="update_profile", weight=42, build=update_profile,
                  check=check_write("Profile updated."), apply=apply_profile),
        Operation(name="set_capacity", weight=12, build=set_capacity,
                  check=check_capacity, apply=apply_capacity),
        Operation(name="add_hotel", weight=8, build=add_hotel,
                  check=check_write("Hotel added."), apply=apply_addition),
    ],
)


# ------------------------------------------------------------ (4) benchmark

def benchmark(run: ms.Run) -> None:
    """The scoring definition: closed-loop capacity, then p99 at 0.7x of it.

    Two closed-loop trials (10 s warmup, 20 s, 32 clients) and three
    open-loop trials (10 s warmup, 60 s, evenly spaced arrivals as the
    servicebench engine sends them), each its own phase; the sample's writes
    carry from phase to phase through the model, so nothing is reset.
    """
    closed = [run.closed_loop(clients=32, secs=20, warmup=10) for _ in range(2)]
    throughput = statistics.median(phase.ops_per_sec for phase in closed)
    # p99 is measured at a fixed fraction of this sample's own throughput, so
    # a faster candidate is held to proportionally more load.
    rate = 0.7 * throughput
    opened = [run.open_loop(rate=rate, secs=60, warmup=10, arrivals="uniform") for _ in range(3)]
    run.report({
        "operations_per_second": throughput,
        "latency_ms.p99": statistics.median(phase.p99_ms for phase in opened),
    })


# ------------------------------------------------------------- (5) validity

if __name__ == "__main__":
    ms.main(
        dataset=ds.generate,
        system=SYSTEM,
        workload=WORKLOAD,
        benchmark=benchmark,
        metrics={
            "operations_per_second": vse.Metric(unit="ops/s", direction="max"),
            "latency_ms.p99": vse.Metric(unit="ms", direction="min"),
        },
        # workload/*.toml [constraints] and min_offered_rate_ratio.
        validity=vse.Validity(max_error_rate=0.0, min_sustained_ratio=0.95, min_ops_per_operation=1),
    )
