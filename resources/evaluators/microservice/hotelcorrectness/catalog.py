"""Pinned public Hotel Reservation fixture catalog."""

# ruff: noqa: PLR2004, TRY003

from __future__ import annotations

import struct
from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    """One externally observable seeded hotel profile."""

    id: str
    name: str
    phone: str
    lat: float
    lon: float
    recommend_lat: float
    recommend_lon: float


def _float32(value: float) -> float:
    return struct.unpack("f", struct.pack("f", value))[0]


_INITIAL_PROFILES = (
    Profile(
        "1",
        "Clift Hotel",
        "(415) 775-4700",
        _float32(37.7867),
        _float32(-122.4112),
        37.7867,
        -122.4112,
    ),
    Profile(
        "2",
        "W San Francisco",
        "(415) 777-5300",
        _float32(37.7854),
        _float32(-122.4005),
        37.7854,
        -122.4005,
    ),
    Profile(
        "3",
        "Hotel Zetta",
        "(415) 543-8555",
        _float32(37.7834),
        _float32(-122.4071),
        37.7834,
        -122.4071,
    ),
    Profile(
        "4",
        "Hotel Vitale",
        "(415) 278-3700",
        _float32(37.7936),
        _float32(-122.3930),
        37.7936,
        -122.3930,
    ),
    Profile(
        "5",
        "Phoenix Hotel",
        "(415) 776-1380",
        _float32(37.7831),
        _float32(-122.4181),
        37.7831,
        -122.4181,
    ),
    Profile(
        "6",
        "St. Regis San Francisco",
        "(415) 284-4000",
        _float32(37.7863),
        _float32(-122.4015),
        37.7863,
        -122.4015,
    ),
)


def seed_profiles() -> dict[str, Profile]:
    """Reproduce the pinned profile and recommendation service fixtures."""
    catalog = {profile.id: profile for profile in _INITIAL_PROFILES}
    for numeric_id in range(7, 81):
        hotel_id = str(numeric_id)
        fraction = _float32(_float32(numeric_id) / _float32(500.0))
        profile_lat = _float32(_float32(37.7835) + _float32(fraction * _float32(3.0)))
        profile_lon = _float32(_float32(-122.41) + _float32(fraction * _float32(4.0)))
        catalog[hotel_id] = Profile(
            hotel_id,
            "St. Regis San Francisco",
            f"(415) 284-40{hotel_id}",
            profile_lat,
            profile_lon,
            37.7835 + numeric_id / 500.0 * 3,
            -122.41 + numeric_id / 500.0 * 4,
        )
    if len(catalog) != 80:
        raise ValueError(f"hotel seed catalog has {len(catalog)} profiles, expected 80")
    return catalog


def rate_backed_ids() -> frozenset[str]:
    """Return the exact hotel IDs with seeded rate plans."""
    return frozenset({"1", "2", "3", *(str(value) for value in range(9, 81, 3))})


def capacity_for_hotel(hotel_id: str) -> int:
    """Return the pinned room capacity for one catalog hotel."""
    try:
        numeric_id = int(hotel_id)
    except ValueError as error:
        raise ValueError(f"invalid hotel ID {hotel_id!r}") from error
    if not 1 <= numeric_id <= 80:
        raise ValueError(f"hotel ID {hotel_id!r} is outside the seeded catalog")
    if numeric_id <= 6 or numeric_id % 3 == 0:
        return 200
    return 300 if numeric_id % 3 == 1 else 250
