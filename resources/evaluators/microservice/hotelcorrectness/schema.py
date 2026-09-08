"""Strict Hotel Reservation response-schema validation."""

# ruff: noqa: ANN401, C901, PLR2004, TC003, TRY003, TRY004

from __future__ import annotations

import json
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .catalog import Profile


@dataclass(frozen=True)
class Feature:
    """Normalized strict GeoJSON feature."""

    id: str
    name: str
    phone: str
    lat: Decimal
    lon: Decimal
    raw: Mapping[str, Any]


def _exact_fields(value: Mapping[str, Any], expected: set[str], where: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{where} fields {sorted(value)} do not match {sorted(expected)}")


def _number(value: Any, where: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{where} must be numeric")
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError(f"{where} must be numeric")
    return number


def _float32(value: float) -> float:
    """Round a number to the profile service's IEEE-754 storage type."""
    return struct.unpack("f", struct.pack("f", value))[0]


def _float32_json_value(value: float) -> Decimal:
    """Reproduce Go's shortest JSON number that round-trips to a float32."""
    for precision in range(1, 10):
        candidate = format(value, f".{precision}g")
        if _float32(float(candidate)) == value:
            return Decimal(candidate)
    raise ValueError(f"float32 value {value!r} has no round-tripping decimal")


def decode_feature_collection(value: Any) -> dict[str, Feature]:
    """Decode a response and reject schema, type, and uniqueness mutations."""
    if not isinstance(value, dict):
        raise ValueError("GeoJSON root must be an object")
    _exact_fields(value, {"type", "features"}, "GeoJSON root")
    if value["type"] != "FeatureCollection":
        raise ValueError("GeoJSON root type must be FeatureCollection")
    items = value["features"]
    if not isinstance(items, list):
        raise ValueError("GeoJSON features must be a list")
    features: dict[str, Feature] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"GeoJSON feature {index} must be an object")
        _exact_fields(item, {"type", "id", "properties", "geometry"}, f"feature {index}")
        hotel_id = item["id"]
        if item["type"] != "Feature" or not isinstance(hotel_id, str) or not hotel_id:
            raise ValueError(f"GeoJSON feature {index} has invalid type or id")
        properties = item["properties"]
        if not isinstance(properties, dict):
            raise ValueError(f"GeoJSON feature {index} properties must be an object")
        _exact_fields(properties, {"name", "phone_number"}, f"feature {index} properties")
        name, phone = properties["name"], properties["phone_number"]
        if not isinstance(name, str) or not name or not isinstance(phone, str) or not phone:
            raise ValueError(f"GeoJSON feature {index} name and phone must be non-empty strings")
        geometry = item["geometry"]
        if not isinstance(geometry, dict):
            raise ValueError(f"GeoJSON feature {index} geometry must be an object")
        _exact_fields(geometry, {"type", "coordinates"}, f"feature {index} geometry")
        coordinates = geometry["coordinates"]
        if (
            geometry["type"] != "Point"
            or not isinstance(coordinates, list)
            or len(coordinates) != 2
        ):
            raise ValueError(f"GeoJSON feature {index} geometry must be a two-coordinate Point")
        if hotel_id in features:
            raise ValueError(f"GeoJSON features contain duplicate hotel ID {hotel_id!r}")
        features[hotel_id] = Feature(
            id=hotel_id,
            name=name,
            phone=phone,
            lon=_number(coordinates[0], f"feature {index} longitude"),
            lat=_number(coordinates[1], f"feature {index} latitude"),
            raw=item,
        )
    return features


def decode_feature_collection_json(value: str) -> dict[str, Feature]:
    """Decode GeoJSON while retaining exact JSON-number values."""

    def reject_constant(constant: str) -> None:
        raise ValueError(f"invalid JSON numeric constant {constant}")

    try:
        decoded = json.loads(
            value,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"response body is not strict JSON: {error}") from error
    return decode_feature_collection(decoded)


def validate_profiles(features: Mapping[str, Feature], catalog: Mapping[str, Profile]) -> None:
    """Require every returned feature to equal its seeded public profile."""
    for hotel_id, feature in features.items():
        expected = catalog.get(hotel_id)
        if expected is None:
            raise ValueError(f"response returned unknown hotel ID {hotel_id!r}")
        actual = (
            feature.name,
            feature.phone,
            feature.lat,
            feature.lon,
        )
        wanted = (
            expected.name,
            expected.phone,
            _float32_json_value(expected.lat),
            _float32_json_value(expected.lon),
        )
        if actual != wanted:
            raise ValueError(f"hotel {hotel_id} profile {actual!r} does not match {wanted!r}")


def exact_ids(features: Mapping[str, Feature], expected: set[str] | frozenset[str]) -> None:
    """Require exact order-independent result membership."""
    actual = set(features)
    if actual != set(expected):
        raise ValueError(f"hotel IDs {sorted(actual)} do not match {sorted(expected)}")
