"""Typed feature-flag values and registries."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from collections.abc import Mapping

FlagT = TypeVar("FlagT", bound=StrEnum)


@dataclass(frozen=True)
class FeatureDefinition:
    """Description and default value for one typed feature flag."""

    description: str
    default: bool = False


class FeatureRegistry(Generic[FlagT]):
    """Registry for a project's typed feature flag manifest."""

    def __init__(
        self,
        flag_type: type[FlagT],
        definitions: Mapping[FlagT, FeatureDefinition],
    ) -> None:
        """Initialize the registry with definitions for one flag enum."""
        self._flag_type = flag_type
        self._definitions = dict(definitions)
        self._validate_definitions()

    @property
    def flag_type(self) -> type[FlagT]:
        """Return the enum type governed by this registry."""
        return self._flag_type

    @property
    def definitions(self) -> Mapping[FlagT, FeatureDefinition]:
        """Return a read-only view of all flag definitions."""
        return MappingProxyType(self._definitions)

    def default_for(self, flag: FlagT) -> bool:
        """Return the configured default for ``flag``."""
        return self._definitions[flag].default

    def is_enabled(
        self,
        flag: FlagT,
        overrides: Mapping[FlagT, bool] | None = None,
    ) -> bool:
        """Resolve a flag using an override when present, otherwise its default."""
        if overrides is not None and flag in overrides:
            return overrides[flag]
        return self.default_for(flag)

    def _validate_definitions(self) -> None:
        for flag in self._definitions:
            if not isinstance(flag, self._flag_type):
                message = f"Feature definition key {flag!r} is not a {self._flag_type.__name__}"
                raise TypeError(message)

        missing = [flag.value for flag in self._flag_type if flag not in self._definitions]
        if missing:
            message = f"Missing feature definitions for: {', '.join(missing)}"
            raise ValueError(message)
