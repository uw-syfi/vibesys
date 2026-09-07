"""Pure sequential compatibility model for the pinned Hotel API."""

# ruff: noqa: PLR2004, TRY003, TRY004

from __future__ import annotations

import binascii
import datetime as dt
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .catalog import Profile, capacity_for_hotel, rate_backed_ids, seed_profiles

LOGIN_SUCCESS = "Login successfully!"
LOGIN_FAILURE = "Failed. Please check your username and password. "
RESERVATION_SUCCESS = "Reserve successfully!"
RESERVATION_FAILURE = "Failed. Already reserved. "


def credentials_for_user(index: int) -> tuple[str, str]:
    """Reproduce one seeded user's intentionally hexadecimal username."""
    if not 0 <= index <= 500:
        raise ValueError(f"hotel user index {index} is outside [0, 500]")
    suffix = str(index)
    username = "Cornell_" + binascii.hexlify(suffix.encode()).decode()
    return username, suffix * 10


def seeded_users() -> dict[str, str]:
    """Reproduce the 501 public Cornell users."""
    return dict(credentials_for_user(index) for index in range(501))


def reservation_nights(query: Mapping[str, str]) -> tuple[str, ...]:
    """Expand the half-open reservation date interval into individual nights."""
    try:
        start = dt.date.fromisoformat(query["inDate"])
        end = dt.date.fromisoformat(query["outDate"])
    except (KeyError, ValueError) as error:
        raise ValueError("reservation dates must be ISO dates") from error
    nights: list[str] = []
    current = start
    while current < end:
        nights.append(current.isoformat())
        current += dt.timedelta(days=1)
    return tuple(nights)


@dataclass
class SequentialModel:
    """Model externally observable login, recommendation, reservation, and search state."""

    catalog: Mapping[str, Profile] = field(default_factory=seed_profiles)
    users: Mapping[str, str] = field(default_factory=seeded_users)
    reserved: dict[tuple[str, str], int] = field(default_factory=dict)

    def apply(self, action: Mapping[str, Any]) -> dict[str, Any]:
        """Apply one `{kind, query}` action and return its normalized observation."""
        kind = action.get("kind")
        query = action.get("query")
        if query is None:
            query = {key: value for key, value in action.items() if key != "kind"}
        if not isinstance(kind, str) or not isinstance(query, Mapping):
            raise ValueError("action requires string kind and optional query mapping")
        normalized = {str(key): str(value) for key, value in query.items()}
        if kind == "login":
            return {"message": self._login(normalized)}
        if kind == "recommend":
            return {"hotel_ids": self._recommend(normalized)}
        if kind == "reserve":
            return {"message": self._reserve(normalized)}
        if kind == "search":
            return {"hotel_ids": self._search(normalized)}
        raise ValueError(f"unknown hotel action kind {kind!r}")

    def _valid_credentials(self, query: Mapping[str, str]) -> bool:
        username = query.get("username", "")
        return bool(username) and self.users.get(username) == query.get("password")

    def _login(self, query: Mapping[str, str]) -> str:
        return LOGIN_SUCCESS if self._valid_credentials(query) else LOGIN_FAILURE

    def _recommend(self, query: Mapping[str, str]) -> list[str]:
        requirement = query.get("require")
        if requirement == "price":
            return ["2"]
        if requirement == "rate":
            return ["9", "24", "39", "54", "69"]
        if requirement != "dis":
            raise ValueError(f"unsupported recommendation requirement {requirement!r}")
        try:
            lat, lon = float(query["lat"]), float(query["lon"])
        except (KeyError, ValueError) as error:
            raise ValueError("recommendation coordinates must be numeric") from error
        nearest = min(
            self.catalog.values(),
            key=lambda profile: math.hypot(
                profile.recommend_lat - lat, profile.recommend_lon - lon
            ),
        )
        return [nearest.id]

    def _reserve(self, query: Mapping[str, str]) -> str:
        hotel_id = query.get("hotelId", "")
        nights = reservation_nights(query)
        raw_rooms = query.get("number", "")
        try:
            rooms = int(raw_rooms) if raw_rooms else 0
        except ValueError as error:
            raise ValueError(f"room count {raw_rooms!r} must be an integer") from error
        capacity = capacity_for_hotel(hotel_id)
        if any(self.reserved.get((hotel_id, night), 0) + rooms > capacity for night in nights):
            return RESERVATION_FAILURE
        for night in nights:
            key = (hotel_id, night)
            self.reserved[key] = self.reserved.get(key, 0) + rooms
        # Preserve the pinned frontend quirk: mutation precedes the auth response.
        return RESERVATION_SUCCESS if self._valid_credentials(query) else LOGIN_FAILURE

    def _search(self, query: Mapping[str, str]) -> list[str]:
        nights = reservation_nights(query)
        available = []
        for hotel_id in sorted(rate_backed_ids(), key=int):
            capacity = capacity_for_hotel(hotel_id)
            if all(self.reserved.get((hotel_id, night), 0) + 1 <= capacity for night in nights):
                available.append(hotel_id)
        return available
