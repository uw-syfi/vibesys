"""The hotel-k8s dataset: hotels, rate plans and users, generated from a seed.

Spec example (abridged where repetitive): it states what moves out of the Go
module `evaluator/internal/dataset` (generator), `internal/appsupport/hotel`
(reference answers) and `internal/seed` (MongoDB documents). Generation is a
pure function of SEED: every entity draws from its own `Rand(SEED, kind, i)`
stream, so adding a field to one kind of record does not reshuffle the others.

Shape (unchanged from the Go generator): 20 cities in popularity order, 25,000
hotels spread over them by Zipf rank (exponent 1), each near its city centre
(normal offsets, 15% on the outskirts), 1-5 rate plans over the 2027 rate
window, a rating 1 + 4 Beta(5, 2), a capacity by star class and a popularity
rank within its city; 10,000 users with unique names and random passwords.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import date, timedelta

from vibesys_evaluator import Rand

# Changing SEED or any constant below changes the task: results measured on
# different datasets do not compare.
SEED = 0x686F74656C2D7631  # "hotel-v1"
HOTEL_COUNT = 25_000
USER_COUNT = 10_000
RATE_WINDOW_START = date(2027, 1, 1)
RATE_WINDOW_DAYS = 365
KM_PER_DEGREE_LAT = 111.32


@dataclass(frozen=True)
class CityTemplate:
    name: str
    region: str
    country: str
    lat: float
    lon: float
    phone: str  # 'x' becomes a digit, 'A' an upper-case letter
    postal: str
    base_price: float
    tax: float
    districts: tuple[str, ...]
    streets: tuple[str, ...]


CITY_TABLE: tuple[CityTemplate, ...] = (
    CityTemplate("London", "England", "United Kingdom", 51.5074, -0.1278, "+44 20 xxxx xxxx",
                 "WAx xAA", 210, 0.20, ("Covent Garden", "Soho", "Mayfair"), ("Strand", "Piccadilly")),
    CityTemplate("Paris", "Ile-de-France", "France", 48.8566, 2.3522, "+33 1 xx xx xx xx",
                 "750xx", 200, 0.10, ("Le Marais", "Montmartre"), ("Rue de Rivoli", "Rue de la Paix")),
    # ... the other 18 cities of evaluator/internal/dataset/cities.go, in popularity order.
)

# The additions' fictional city: hotels that add_hotel writes lie in its
# region (the South Pacific), more than 1,000 km from every seeded city.
ADDITION_CITY = CityTemplate("Isla Nueva", "Pacifico", "Chile", -40, -125, "+56 32 xxx xxxx",
                             "2xxxxxx", 140, 0.19, ("the Harbour", "Playa Norte"), ("Av del Mar",))
CELL_COUNT = 21_000  # cells of 25 km in the additions' region (computed in the full module)
CELL_RADIUS_KM = 2.0


@dataclass(frozen=True)
class RatePlan:
    code: str
    in_date: str
    out_date: str
    room_code: str
    room_description: str
    bookable_rate: float
    total_rate: float
    total_rate_inclusive: float


@dataclass(frozen=True)
class Hotel:
    id: str
    city: int  # -1 for an added hotel
    name: str
    phone: str
    lat: float
    lon: float
    stars: int
    rating: float
    price: float
    capacity: int
    rate_plans: tuple[RatePlan, ...]
    description: str = ""
    address: tuple[str, ...] = ()  # street number, street, city, state, country, postal code


@dataclass(frozen=True)
class User:
    username: str
    password: str


@dataclass(frozen=True)
class City:
    template: CityTemplate
    spread_km: float
    km_per_degree_lon: float
    hotels: tuple[int, ...]  # indices into Dataset.hotels, most popular first


@dataclass(frozen=True)
class Dataset:
    cities: tuple[City, ...]
    hotels: tuple[Hotel, ...]  # hotels[i].id == str(i + 1)
    users: tuple[User, ...]
    by_id: dict[str, Hotel] = field(default_factory=dict)

    def digest(self) -> str:
        """SHA-256 over a canonical encoding; the seed Job writes it as a marker."""
        h = hashlib.sha256()
        for hotel in self.hotels:
            h.update(repr(hotel).encode())
        for user in self.users:
            h.update(repr(user).encode())
        return h.hexdigest()


STAR_WEIGHTS = (8, 20, 35, 25, 12)
STAR_PRICE = (0.45, 0.65, 1.0, 1.6, 2.8)
STAR_ROOMS = ((10, 60), (30, 150), (60, 300), (100, 450), (150, 800))
PLAN_WEIGHTS = (20, 30, 25, 15, 10)
PLAN_CODES = (("RACK", 1.0), ("BAR", 0.92), ("AAA", 0.88), ("GOV", 0.85), ("ADV", 0.8), ("PKG", 1.15))
ROOMS = (("KNG", "King sized bed", 1.0), ("QN", "Queen sized bed", 0.9), ("DBL", "Two double beds", 1.05))


def generate() -> Dataset:
    """The task's dataset (about 1 s); the seed Job and every phase pod call it."""
    counts = zipf_counts(HOTEL_COUNT, len(CITY_TABLE))
    spreads = [1.5 + 2.5 * math.sqrt(c / counts[0]) for c in counts]
    assignment = [city for city, count in enumerate(counts) for _ in range(count)]
    shuffle(Rand(SEED, "assignment"), assignment)
    members: list[list[int]] = [[] for _ in CITY_TABLE]
    taken: set[tuple[float, float]] = set()
    hotels = []
    for index, city in enumerate(assignment):
        members[city].append(index)
        hotels.append(generate_hotel(Rand(SEED, "hotel", index), index, city, spreads[city], taken))
    cities = []
    for city, template in enumerate(CITY_TABLE):
        order = members[city]
        shuffle(Rand(SEED, "popularity", city), order)
        cities.append(City(template, spreads[city],
                           KM_PER_DEGREE_LAT * math.cos(math.radians(template.lat)), tuple(order)))
    users = generate_users(Rand(SEED, "users"))
    hotels_t = tuple(hotels)
    return Dataset(tuple(cities), hotels_t, users, {h.id: h for h in hotels_t})


def generate_hotel(r: Rand, index: int, city: int, spread_km: float,
                   taken: set[tuple[float, float]]) -> Hotel:
    template = CITY_TABLE[city]
    stars = 1 + r.weighted(STAR_WEIGHTS)
    spread = spread_km * (2.5 if r.random() < 0.15 else 1.0)
    km_per_lon = KM_PER_DEGREE_LAT * math.cos(math.radians(template.lat))
    while True:
        lat = round(template.lat + spread * r.normal() / KM_PER_DEGREE_LAT, 6)
        lon = round(template.lon + spread * r.normal() / km_per_lon, 6)
        if (lat, lon) not in taken:
            taken.add((lat, lon))
            break
    return describe(r, str(index + 1), city, template, stars, lat, lon)


def describe(r: Rand, hotel_id: str, city: int, template: CityTemplate, stars: int,
             lat: float, lon: float) -> Hotel:
    """Everything of a hotel after its location: name, phone, rating, capacity, plans."""
    name = f"Hotel {template.districts[r.below(len(template.districts))]}"  # abridged naming
    rating = round(1 + 4 * sorted(r.random() for _ in range(6))[4], 2)
    low, high = STAR_ROOMS[stars - 1]
    capacity = low + int((high - low) * (r.random() + r.random()) / 2)
    base = template.base_price * STAR_PRICE[stars - 1] * (0.75 + 0.25 * (r.random() + r.random()))
    plans = []
    for n in range(1 + r.weighted(PLAN_WEIGHTS)):
        code, factor = PLAN_CODES[0] if n == 0 else PLAN_CODES[1 + (n - 1 + r.below(5 - n + 1)) % 5]
        days = RATE_WINDOW_DAYS if n == 0 else 14 + r.below(167)
        start = 0 if n == 0 else r.below(RATE_WINDOW_DAYS - days + 1)
        room_code, room_description, room_factor = ROOMS[r.below(len(ROOMS))]
        total = round(base * factor * room_factor, 2)
        plans.append(RatePlan(
            code, str(RATE_WINDOW_START + timedelta(start)),
            str(RATE_WINDOW_START + timedelta(start + days)), room_code, room_description,
            total, total, round(total * (1 + template.tax), 2)))
    return Hotel(hotel_id, city, name, fill(r, template.phone), lat, lon, stars, rating,
                 min(p.total_rate for p in plans), capacity, tuple(plans))


def generate_users(r: Rand) -> tuple[User, ...]:
    seen: set[str] = set()
    users = []
    while len(users) < USER_COUNT:
        name = f"user{r.below(10**9)}"  # abridged: first.last[digits] in the full module
        if name in seen:
            continue
        seen.add(name)
        users.append(User(name, "".join("abcdefghijkmnpqrstuvwxyz23456789"[r.below(32)]
                                        for _ in range(10 + r.below(7)))))
    return tuple(users)


# -- values that writes put into the application (not part of the seeded data) --

def new_phone(hotel: Hotel, r: Rand) -> str:
    return fill(r, (CITY_TABLE[hotel.city] if hotel.city >= 0 else ADDITION_CITY).phone)


def new_name(hotel: Hotel, r: Rand) -> str:
    template = CITY_TABLE[hotel.city] if hotel.city >= 0 else ADDITION_CITY
    return f"Hotel {template.districts[r.below(len(template.districts))]} {r.below(100)}"


def cell_point(cell: int, r: Rand) -> tuple[float, float]:
    """A location within CELL_RADIUS_KM of additions cell `cell`'s centre."""
    lat0, lon0 = -58.0 + (cell // 140 + 0.5) * 25 / KM_PER_DEGREE_LAT, -165.0 + (cell % 140 + 0.5) * 0.38
    d = CELL_RADIUS_KM / math.sqrt(2)
    return (round(lat0 + (2 * r.random() - 1) * d / KM_PER_DEGREE_LAT, 6),
            round(lon0 + (2 * r.random() - 1) * d / (KM_PER_DEGREE_LAT * math.cos(math.radians(lat0))), 6))


def added_hotel(hotel_id: str, r: Rand, lat: float, lon: float) -> Hotel:
    return describe(r, hotel_id, -1, ADDITION_CITY, 1 + r.weighted(STAR_WEIGHTS), lat, lon)


# -- reference answers: what the pinned application returns for this dataset --

class Reference:
    """Expected answers of the read paths that depend only on data.

    search_candidates reproduces the geo service's go-geoindex answer (the 5
    nearest hotels within 10 km on a 0.5 km grid, approximate) operation for
    operation; the full module is the port of appsupport/hotel/geoindex.go
    (151 lines) and reference.go.
    """

    def __init__(self, data: Dataset, added: tuple[Hotel, ...] = ()) -> None:
        self.data = data
        self.added = added
        self.max_rating = max(h.rating for h in data.hotels)
        self.min_price = min(h.price for h in data.hotels)

    def search_candidates(self, lat: str, lon: str,
                          added: tuple[Hotel, ...] = ()) -> tuple[list[str], bool]:
        """Geo's nearest rate-backed hotels for a query; exact is False on a tie."""
        raise NotImplementedError("port of appsupport/hotel/geoindex.go")

    def nearest(self, lat: float, lon: float, added: tuple[Hotel, ...] = ()) -> list[str]:
        """The "dis" recommendation: every hotel at the minimum haversine distance."""
        raise NotImplementedError("port of reference.go NearestHotelsWith")

    def best_rated(self) -> list[str]:
        return [h.id for h in self.data.hotels if h.rating == self.max_rating]

    def cheapest(self) -> list[str]:
        return [h.id for h in self.data.hotels if h.price == self.min_price]


# -- MongoDB documents (what the services themselves store) --

def documents(data: Dataset) -> dict[str, dict[str, list[dict[str, object]]]]:
    """database -> collection -> documents, with the services' field names.

    Profile coordinates are float32 values and capacities int32 in the
    services' own schema; the seed function converts with bson types.
    """
    return {
        "geo-db": {"geo": [{"hotelId": h.id, "lat": h.lat, "lon": h.lon} for h in data.hotels]},
        "profile-db": {"hotels": [{"id": h.id, "name": h.name, "phoneNumber": h.phone,
                                   "description": h.description} for h in data.hotels]},
        "rate-db": {"inventory": [
            {"hotelId": h.id, "code": p.code, "inDate": p.in_date, "outDate": p.out_date,
             "roomType": {"bookableRate": p.bookable_rate, "code": p.room_code,
                          "roomDescription": p.room_description, "totalRate": p.total_rate,
                          "totalRateInclusive": p.total_rate_inclusive}}
            for h in data.hotels for p in h.rate_plans]},
        "recommendation-db": {"recommendation": [
            {"hotelId": h.id, "lat": h.lat, "lon": h.lon, "rate": h.rating, "price": h.price}
            for h in data.hotels]},
        "reservation-db": {"number": [{"hotelId": h.id, "numberOfRoom": h.capacity} for h in data.hotels]},
        "user-db": {"user": [{"username": u.username,
                              "password": hashlib.sha256(u.password.encode()).hexdigest()}
                             for u in data.users]},
    }


# -- helpers --

def zipf_counts(total: int, n: int) -> list[int]:
    """total split over n ranks in proportion to 1/rank, by largest remainder."""
    weights = [1 / (k + 1) for k in range(n)]
    shares = [total * w / sum(weights) for w in weights]
    counts = [int(s) for s in shares]
    for k in sorted(range(n), key=lambda k: counts[k] - shares[k])[: total - sum(counts)]:
        counts[k] += 1
    return counts


def shuffle(r: Rand, items: list[int]) -> None:
    for i in range(len(items) - 1, 0, -1):
        j = r.below(i + 1)
        items[i], items[j] = items[j], items[i]


def fill(r: Rand, template: str) -> str:
    return "".join(str(r.below(10)) if c == "x" else chr(65 + r.below(26)) if c == "A" else c
                   for c in template)
