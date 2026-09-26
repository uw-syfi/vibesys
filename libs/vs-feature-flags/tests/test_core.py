from enum import StrEnum
from typing import TYPE_CHECKING, cast

import pytest

from vs_feature_flags.api import FeatureDefinition, FeatureRegistry

if TYPE_CHECKING:
    from collections.abc import MutableMapping


class ExampleFlag(StrEnum):
    NEW_LOOP = "new_loop"
    STRICT_MODE = "strict_mode"


class OtherFlag(StrEnum):
    OTHER = "other"


def _registry() -> FeatureRegistry[ExampleFlag]:
    return FeatureRegistry(
        ExampleFlag,
        {
            ExampleFlag.NEW_LOOP: FeatureDefinition(
                description="Use the new loop.",
                default=False,
            ),
            ExampleFlag.STRICT_MODE: FeatureDefinition(
                description="Use stricter behavior.",
                default=True,
            ),
        },
    )


def test_registry_uses_defaults() -> None:
    registry = _registry()

    assert registry.is_enabled(ExampleFlag.NEW_LOOP) is False
    assert registry.is_enabled(ExampleFlag.STRICT_MODE) is True


def test_registry_uses_typed_overrides() -> None:
    registry = _registry()

    assert registry.is_enabled(ExampleFlag.NEW_LOOP, {ExampleFlag.NEW_LOOP: True}) is True
    assert registry.is_enabled(ExampleFlag.STRICT_MODE, {ExampleFlag.STRICT_MODE: False}) is False


def test_registry_requires_every_enum_member_to_have_definition() -> None:
    with pytest.raises(ValueError, match="Missing feature definitions for: strict_mode"):
        FeatureRegistry(
            ExampleFlag,
            {
                ExampleFlag.NEW_LOOP: FeatureDefinition(description="Use the new loop."),
            },
        )


def test_registry_rejects_definitions_for_another_enum() -> None:
    with pytest.raises(TypeError, match="is not a ExampleFlag"):
        FeatureRegistry(
            ExampleFlag,
            {
                ExampleFlag.NEW_LOOP: FeatureDefinition(description="Use the new loop."),
                ExampleFlag.STRICT_MODE: FeatureDefinition(description="Use stricter behavior."),
                OtherFlag.OTHER: FeatureDefinition(description="Wrong enum."),
            },
        )


def test_definitions_mapping_is_read_only() -> None:
    registry = _registry()
    # The property hands out a read-only view. Take it as mutable so the
    # assignment below is the runtime behavior under test, not a type error.
    definitions = cast("MutableMapping[ExampleFlag, FeatureDefinition]", registry.definitions)

    with pytest.raises(TypeError):
        definitions[ExampleFlag.NEW_LOOP] = FeatureDefinition(description="Changed.")
