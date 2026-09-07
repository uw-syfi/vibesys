"""Reusable normalization helpers for nondeterministic JSON responses."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import JsonValue


def normalize_json(
    value: JsonValue,
    *,
    ignore_keys: frozenset[str] = frozenset(),
    unordered_paths: frozenset[tuple[str, ...]] = frozenset(),
    _path: tuple[str, ...] = (),
) -> JsonValue:
    """Normalize ignored object keys and selected unordered array paths."""
    if isinstance(value, dict):
        return {
            key: normalize_json(
                item,
                ignore_keys=ignore_keys,
                unordered_paths=unordered_paths,
                _path=(*_path, key),
            )
            for key, item in sorted(value.items())
            if key not in ignore_keys
        }
    if isinstance(value, list):
        normalized = [
            normalize_json(
                item,
                ignore_keys=ignore_keys,
                unordered_paths=unordered_paths,
                _path=(*_path, "[]"),
            )
            for item in value
        ]
        if _path in unordered_paths:
            normalized.sort(
                key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"))
            )
        return normalized
    return value


def json_equal(
    left: JsonValue,
    right: JsonValue,
    *,
    ignore_keys: frozenset[str] = frozenset(),
    unordered_paths: frozenset[tuple[str, ...]] = frozenset(),
) -> bool:
    """Compare JSON values after deterministic normalization."""
    return normalize_json(
        left, ignore_keys=ignore_keys, unordered_paths=unordered_paths
    ) == normalize_json(right, ignore_keys=ignore_keys, unordered_paths=unordered_paths)
