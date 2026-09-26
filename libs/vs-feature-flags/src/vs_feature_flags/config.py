"""Configuration helpers for typed feature-flag registries."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import TypeVar

FlagT = TypeVar("FlagT", bound=StrEnum)


def parse_feature_flag_overrides(
    raw: object,
    flag_type: type[FlagT],
    *,
    section_name: str = "feature_flags",
) -> dict[FlagT, bool]:
    """Parse user config into typed feature flag overrides.

    The library accepts only TOML-style boolean values. It intentionally does
    not coerce strings like ``"true"`` so config mistakes fail early.
    """
    if raw is None:
        return {}

    if not isinstance(raw, Mapping):
        message = f"{section_name} must be a TOML table"
        raise ValueError(message)  # noqa: TRY004  # lint-waiver: LW-010112 [TRY004]; malformed user configuration is part of the ValueError config-error API.

    parsed: dict[FlagT, bool] = {}
    for key, value in raw.items():
        try:
            flag = flag_type(str(key))
        except (TypeError, ValueError):
            valid = _format_valid_flags(flag_type)
            message = f"Unknown feature flag {key!r}. Supported flags: {valid}"
            raise ValueError(message) from None

        if not isinstance(value, bool):
            message = f"{section_name}.{key} must be true or false"
            raise ValueError(message)  # noqa: TRY004  # lint-waiver: LW-010113 [TRY004]; invalid feature-flag values remain ValueError config errors for callers.

        parsed[flag] = value

    return parsed


def _format_valid_flags(flag_type: type[FlagT]) -> str:
    values = [flag.value for flag in flag_type]
    if not values:
        return "none"
    return ", ".join(values)
