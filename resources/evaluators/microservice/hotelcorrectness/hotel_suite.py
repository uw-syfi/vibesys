"""Deterministic, stateful correctness conditions for Hotel Reservation."""

# ruff: noqa: ANN401, C901, D102, PERF401, PLR0911, PLR0912, PLR0913, TRY003, TRY301

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any

from vs_correctness import (
    CustomAction,
    Decision,
    HTTPAction,
    OracleContext,
    Suite,
    TestCase,
    Verdict,
)

from .catalog import capacity_for_hotel, rate_backed_ids, seed_profiles
from .model import SequentialModel, credentials_for_user
from .schema import decode_feature_collection_json, exact_ids, validate_profiles

FIXED_CASES = 4
REQUIRED_PROPERTIES = frozenset(
    {
        "protocol_contract",
        "persistent_http",
        "strict_geojson_schema",
        "seeded_profile_semantics",
        "search_semantics",
        "search_availability",
        "recommendation_semantics",
        "authentication_semantics",
        "sequential_differential",
        "reservation_optional_number",
        "reservation_capacity",
        "read_your_write",
        "reservation_isolation",
        "crash_recovery",
    }
)


def _dates(day: dt.date) -> tuple[str, str]:
    return day.isoformat(), (day + dt.timedelta(days=1)).isoformat()


def _search_query(day: dt.date, hotel_id: str = "1") -> dict[str, str]:
    profile = seed_profiles()[hotel_id]
    in_date, out_date = _dates(day)
    return {
        "inDate": in_date,
        "outDate": out_date,
        "lat": str(profile.recommend_lat),
        "lon": str(profile.recommend_lon),
    }


def _reservation_query(
    hotel_id: str,
    day: dt.date,
    rooms: int | None,
    customer: str,
    user_index: int = 30,
    *,
    valid_password: bool = True,
) -> dict[str, str]:
    username, password = credentials_for_user(user_index)
    in_date, out_date = _dates(day)
    query = {
        "hotelId": hotel_id,
        "inDate": in_date,
        "outDate": out_date,
        "customerName": customer,
        "username": username,
        "password": password if valid_password else f"{password}-wrong",
    }
    if rooms is not None:
        query["number"] = str(rooms)
    return query


def _model_action(kind: str, query: dict[str, str]) -> HTTPAction:
    path = {
        "login": "/user",
        "recommend": "/recommendations",
        "reserve": "/reservation",
        "search": "/hotels",
    }[kind]
    return HTTPAction(method="GET", path=path, query=query)


@dataclass(frozen=True)
class HotelGenerator:
    """Generate four fixed contracts plus the requested random model histories."""

    def generate(self, context: Any) -> list[TestCase]:
        if context.cases < FIXED_CASES:
            raise ValueError(f"hotel suite requires at least {FIXED_CASES} cases")
        random = context.random()
        epoch = dt.date(2400, 1, 1) + dt.timedelta(days=random.randrange(20_000))
        cases = [
            self._catalog_case(context.seed, random),
            self._protocol_case(context.seed),
            self._reservation_case(context.seed, epoch),
            self._crash_case(context.seed, epoch + dt.timedelta(days=100)),
        ]
        rate_ids = sorted(rate_backed_ids(), key=int)
        random.shuffle(rate_ids)
        for index in range(context.cases - FIXED_CASES):
            cases.append(
                self._sequential_case(
                    context.seed,
                    index,
                    epoch + dt.timedelta(days=200 + index * 4),
                    rate_ids[index % len(rate_ids)],
                    rate_ids[(index + 1) % len(rate_ids)],
                    random,
                )
            )
        return cases

    @staticmethod
    def _case(case_id: str, actions: list[Any]) -> TestCase:
        return TestCase(id=case_id, actions=tuple(actions))

    def _catalog_case(self, seed: int, random: Any) -> TestCase:
        profiles = seed_profiles()
        actions: list[Any] = []
        for offset in range(8):
            in_day = random.randrange(9, 24)
            out_day = random.randrange(in_day + 1, 25)
            profile = profiles[str(random.randrange(7, 81))]
            jitter = random.uniform(-0.001, 0.001)
            search = {
                "inDate": dt.date(2015, 4, in_day).isoformat(),
                "outDate": dt.date(2015, 4, out_day).isoformat(),
                "lat": str(profile.recommend_lat + jitter),
                "lon": str(profile.recommend_lon - jitter),
            }
            if offset % 2:
                search["locale"] = "en"
            for _ in range(2):
                actions.append(HTTPAction(method="GET", path="/hotels", query=search))
        for requirement in ("price", "rate"):
            actions.append(
                HTTPAction(
                    method="GET",
                    path="/recommendations",
                    query={"require": requirement, "lat": "37.7867", "lon": "-122.4112"},
                )
            )
        for profile in profiles.values():
            for jitter in (0.0, 0.000001):
                actions.append(
                    HTTPAction(
                        method="GET",
                        path="/recommendations",
                        query={
                            "require": "dis",
                            "lat": str(profile.recommend_lat + jitter),
                            "lon": str(profile.recommend_lon - jitter),
                            "locale": "en",
                        },
                    )
                )
        return self._case(f"hotel-catalog-{seed}", actions)

    def _protocol_case(self, seed: int) -> TestCase:
        actions: list[Any] = []
        for index in range(501):
            username, password = credentials_for_user(index)
            actions.append(_model_action("login", {"username": username, "password": password}))
        for index in (0, 1, 12, 30, 99, 255, 499, 500):
            username, password = credentials_for_user(index)
            actions.append(
                _model_action("login", {"username": username, "password": f"{password}-wrong"})
            )
        actions.append(
            _model_action("login", {"username": "Cornell_missing", "password": "missing"})
        )
        actions.append(CustomAction(name="persistent-http"))
        username, _password = credentials_for_user(30)
        reservation_base = _reservation_query("1", dt.date(2400, 1, 1), 1, "negative", 30)
        malformed_reservations: list[tuple[str, dict[str, str]]] = []
        for missing in ("inDate", "hotelId", "customerName", "password"):
            query = dict(reservation_base)
            del query[missing]
            malformed_reservations.append(("/reservation", query))
        invalid_date = dict(reservation_base)
        invalid_date["inDate"] = "not-a-date"
        malformed = (
            ("/hotels", {"lat": "37.7", "lon": "-122.4"}),
            ("/hotels", {"inDate": "2400-01-01", "outDate": "2400-01-02"}),
            ("/recommendations", {"require": "rate"}),
            ("/recommendations", {"require": "unknown", "lat": "37.7", "lon": "-122.4"}),
            ("/user", {"username": username}),
            ("/reservation", invalid_date),
            *malformed_reservations,
        )
        actions.extend(HTTPAction(method="GET", path=path, query=query) for path, query in malformed)
        return self._case(f"hotel-protocol-{seed}", actions)

    def _reservation_case(self, seed: int, day: dt.date) -> TestCase:
        primary, isolated = "9", "12"
        capacity = capacity_for_hotel(primary)
        actions: list[Any] = []
        for query in (
            _reservation_query(primary, day, None, "optional"),
            _reservation_query(primary, day, capacity, "exact"),
            _reservation_query(primary, day, 1, "over"),
        ):
            actions.append(_model_action("reserve", query))
        adjacent = day + dt.timedelta(days=2)
        query = _reservation_query(primary, adjacent, capacity + 1, "atomic")
        query["outDate"] = (adjacent + dt.timedelta(days=2)).isoformat()
        after_atomic = _reservation_query(primary, adjacent, 1, "after")
        after_atomic["outDate"] = (adjacent + dt.timedelta(days=2)).isoformat()
        actions.extend(
            [
                _model_action("reserve", query),
                _model_action("reserve", after_atomic),
                _model_action("reserve", _reservation_query(isolated, adjacent, 1, "isolated")),
            ]
        )
        split_day = day + dt.timedelta(days=5)
        actions.extend(
            [
                _model_action("search", _search_query(split_day, primary)),
                _model_action("reserve", _reservation_query(primary, split_day, 73, "split-a")),
                _model_action(
                    "reserve", _reservation_query(primary, split_day, capacity - 73, "split-b")
                ),
                _model_action("search", _search_query(split_day, primary)),
                _model_action("reserve", _reservation_query(primary, split_day, 1, "split-over")),
                _model_action("search", _search_query(split_day + dt.timedelta(days=1), primary)),
            ]
        )
        auth_day = day + dt.timedelta(days=8)
        actions.extend(
            [
                _model_action(
                    "reserve",
                    _reservation_query(
                        primary, auth_day, capacity, "invalid-auth", valid_password=False
                    ),
                ),
                _model_action("reserve", _reservation_query(primary, auth_day, 1, "auth-readback")),
                _model_action("search", _search_query(auth_day, primary)),
            ]
        )
        return self._case(f"hotel-reservations-{seed}", actions)

    def _crash_case(self, seed: int, day: dt.date) -> TestCase:
        hotel_id = "24"
        actions = [
            _model_action(
                "reserve",
                _reservation_query(
                    hotel_id, day, capacity_for_hotel(hotel_id), "crash-persistence"
                ),
            ),
            CustomAction(name="restart-services"),
            _model_action("reserve", _reservation_query(hotel_id, day, 1, "after-restart")),
            _model_action("search", _search_query(day, hotel_id)),
        ]
        return self._case(f"hotel-crash-recovery-{seed}", actions)

    def _sequential_case(
        self, seed: int, index: int, day: dt.date, primary: str, isolated: str, random: Any
    ) -> TestCase:
        capacity = capacity_for_hotel(primary)
        first = random.randrange(1, capacity)
        user_index = random.randrange(501)
        username, password = credentials_for_user(user_index)
        profile = seed_profiles()[primary]
        actions = [
            _model_action("login", {"username": username, "password": password}),
            _model_action("login", {"username": username, "password": f"{password}-wrong"}),
            _model_action(
                "recommend",
                {
                    "require": "price",
                    "lat": str(profile.recommend_lat),
                    "lon": str(profile.recommend_lon),
                },
            ),
            _model_action(
                "recommend",
                {
                    "require": "rate",
                    "lat": str(profile.recommend_lat),
                    "lon": str(profile.recommend_lon),
                    "locale": "en",
                },
            ),
            _model_action(
                "recommend",
                {
                    "require": "dis",
                    "lat": str(profile.recommend_lat),
                    "lon": str(profile.recommend_lon),
                },
            ),
            _model_action("search", _search_query(day, primary)),
            _model_action(
                "reserve", _reservation_query(primary, day, first, f"seq-{index}-a", user_index)
            ),
            _model_action(
                "reserve",
                _reservation_query(primary, day, capacity - first, f"seq-{index}-b", user_index),
            ),
            _model_action("search", _search_query(day, primary)),
            _model_action(
                "reserve", _reservation_query(primary, day, 1, f"seq-{index}-over", user_index)
            ),
            _model_action(
                "reserve", _reservation_query(isolated, day, 1, f"seq-{index}-isolated", user_index)
            ),
            _model_action(
                "reserve",
                _reservation_query(
                    primary,
                    day + dt.timedelta(days=2),
                    capacity,
                    f"seq-{index}-invalid",
                    user_index,
                    valid_password=False,
                ),
            ),
            _model_action(
                "reserve",
                _reservation_query(
                    primary, day + dt.timedelta(days=2), 1, f"seq-{index}-auth-readback", user_index
                ),
            ),
            _model_action("search", _search_query(day + dt.timedelta(days=2), primary)),
        ]
        return self._case(f"hotel-sequential-{seed}-{index}", actions)


class HotelOracle:
    """Validate transport results against strict schemas and the independent model."""

    properties = REQUIRED_PROPERTIES

    def check(self, context: OracleContext) -> Decision:
        services = context.candidate.artifacts.get("compose_services")
        if not isinstance(services, list) or not all(isinstance(item, str) for item in services):
            return Decision(
                verdict=Verdict.INCONCLUSIVE,
                reason="observation does not contain the default Compose service topology",
            )
        if "jaeger" not in services:
            return Decision(
                verdict=Verdict.FAIL,
                reason="default Compose topology does not contain the required jaeger service",
            )
        results = [item for item in context.candidate.action_results if item.phase == "actions"]
        failed = next((item for item in results if item.error is not None), None)
        if failed is not None:
            return Decision(
                verdict=Verdict.INCONCLUSIVE,
                reason=f"action {failed.index} execution failed: {failed.error}",
            )
        actions = context.test_case.actions
        if len(results) != len(actions):
            return Decision(
                verdict=Verdict.FAIL,
                reason=f"expected {len(actions)} action results, got {len(results)}",
            )
        model, catalog = SequentialModel(), seed_profiles()
        try:
            for index, (action, result) in enumerate(zip(actions, results, strict=True)):
                if result.index != index:
                    raise ValueError(
                        f"action result index {result.index}, expected sequence index {index}"
                    )
                if isinstance(action, CustomAction):
                    if action.name not in {"persistent-http", "restart-services"}:
                        raise ValueError(f"action {index} has unknown custom action {action.name!r}")
                    self._status(result, 204, index)
                    continue
                model_action = self._classify_http(action)
                if model_action is None:
                    self._status(result, 400, index)
                    continue
                wanted = model.apply(model_action)
                if "message" in wanted:
                    self._message(result, str(wanted["message"]), index)
                    continue
                features = self._features(result, index)
                validate_profiles(features, catalog)
                exact_ids(features, set(wanted["hotel_ids"]))
        except (KeyError, TypeError, ValueError) as error:
            return Decision(verdict=Verdict.FAIL, reason=str(error))
        return Decision(verdict=Verdict.PASS)

    @classmethod
    def _classify_http(cls, action: HTTPAction) -> dict[str, Any] | None:
        """Translate one executable HTTP request into model input or malformed status."""
        if action.method.upper() != "GET" or action.body is not None:
            return None
        query = cls._plain_query(action.query)
        if query is None:
            return None
        if action.path == "/user":
            required = {"username", "password"}
            kind = "login"
        elif action.path == "/recommendations":
            required = {"require", "lat", "lon"}
            kind = "recommend"
            if query.get("require") not in {"price", "rate", "dis"}:
                return None
            if not cls._coordinates(query):
                return None
        elif action.path == "/hotels":
            required = {"inDate", "outDate", "lat", "lon"}
            kind = "search"
            if not cls._coordinates(query) or not cls._dates_valid(query):
                return None
        elif action.path == "/reservation":
            required = {
                "hotelId",
                "inDate",
                "outDate",
                "customerName",
                "username",
                "password",
            }
            kind = "reserve"
            if not cls._dates_valid(query):
                return None
            try:
                if "number" in query:
                    int(query["number"])
            except ValueError:
                return None
        else:
            raise ValueError(f"unsupported hotel HTTP path {action.path!r}")
        if not required <= query.keys() or any(not query[key] for key in required):
            return None
        return {"kind": kind, "query": query}

    @staticmethod
    def _plain_query(query: dict[str, Any]) -> dict[str, str] | None:
        if not all(isinstance(value, str) for value in query.values()):
            return None
        return {key: value for key, value in query.items() if isinstance(value, str)}

    @staticmethod
    def _coordinates(query: dict[str, str]) -> bool:
        try:
            return math.isfinite(float(query["lat"])) and math.isfinite(float(query["lon"]))
        except (KeyError, ValueError):
            return False

    @staticmethod
    def _dates_valid(query: dict[str, str]) -> bool:
        try:
            return dt.date.fromisoformat(query["inDate"]) < dt.date.fromisoformat(query["outDate"])
        except (KeyError, ValueError):
            return False

    @staticmethod
    def _status(result: Any, expected: int, index: int) -> None:
        if result.status != expected:
            raise ValueError(f"action {index} status {result.status}, expected {expected}")

    @classmethod
    def _message(cls, result: Any, expected: str, index: int) -> None:
        cls._status(result, 200, index)
        value = result.json_body()
        if value != {"message": expected}:
            raise ValueError(f"action {index} message {value!r}, expected {expected!r}")

    @classmethod
    def _features(cls, result: Any, index: int) -> Any:
        cls._status(result, 200, index)
        return decode_feature_collection_json(result.body)


SUITE = Suite(generator=HotelGenerator(), oracle=HotelOracle())
