from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Generic, TypeVar

FlagT = TypeVar("FlagT", bound=StrEnum)


@dataclass(frozen=True)
class FeatureDefinition:
    description: str
    default: bool = False


class FeatureRegistry(Generic[FlagT]):
    """Registry for a project's typed feature flag manifest."""

    def __init__(
        self,
        flag_type: type[FlagT],
        definitions: Mapping[FlagT, FeatureDefinition],
    ) -> None:
        self._flag_type = flag_type
        self._definitions = dict(definitions)
        self._validate_definitions()

    @property
    def flag_type(self) -> type[FlagT]:
        return self._flag_type

    @property
    def definitions(self) -> Mapping[FlagT, FeatureDefinition]:
        return MappingProxyType(self._definitions)

    def default_for(self, flag: FlagT) -> bool:
        return self._definitions[flag].default

    def is_enabled(
        self,
        flag: FlagT,
        overrides: Mapping[FlagT, bool] | None = None,
    ) -> bool:
        if overrides is not None and flag in overrides:
            return overrides[flag]
        return self.default_for(flag)

    def _validate_definitions(self) -> None:
        for flag in self._definitions:
            if not isinstance(flag, self._flag_type):
                raise TypeError(
                    f"Feature definition key {flag!r} is not a {self._flag_type.__name__}"
                )

        missing = [flag.value for flag in self._flag_type if flag not in self._definitions]
        if missing:
            raise ValueError(f"Missing feature definitions for: {', '.join(missing)}")
