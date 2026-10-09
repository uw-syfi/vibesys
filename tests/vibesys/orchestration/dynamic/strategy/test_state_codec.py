"""The strategy state persists through the core envelope codec unchanged."""

from __future__ import annotations

from tests.vibesys.orchestration.dynamic.strategy._harness import envelope, round_trip
from tests.vibesys.orchestration.dynamic.strategy._views import snapshot

from vibesys.orchestration.dynamic.strategy.api import (
    STATE_SCHEMA,
    DynamicStrategyState,
    dynamic_operation_registry,
)


def test_initial_state_round_trips_through_envelope() -> None:
    codec, saved = envelope()
    assert round_trip(codec, saved) == saved


def test_populated_state_round_trips_through_envelope() -> None:
    state = DynamicStrategyState(parents=(snapshot(79.835, 1), snapshot(72.564, 2)))
    codec, saved = envelope(state)
    assert round_trip(codec, saved).strategy == state


def test_state_schema_version_matches_declaration() -> None:
    assert DynamicStrategyState().schema_version == STATE_SCHEMA.version


def test_registry_declares_the_four_dynamic_operations() -> None:
    kinds = {item.kind for item in dynamic_operation_registry().descriptors}
    assert kinds == {
        "dynamic.render_role_artifacts",
        "dynamic.verify_parent_revision",
        "dynamic.interpret_evidence",
        "dynamic.retain_verified_revision",
    }
