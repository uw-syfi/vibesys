"""Envelope harness: a started run on the strategy's own declaration and a persisted-state round trip."""

from __future__ import annotations

from tests.vibesys.orchestration.dynamic.strategy._run import FACTS, config

from vibesys.orchestration.dynamic.strategy.api import (
    STATE_SCHEMA,
    DynamicStrategy,
    DynamicStrategyState,
    dynamic_operation_registry,
)
from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    EventCursor,
    HostFence,
    HostId,
    OperationRegistry,
    RunEnvelope,
)
from vs_core.testing.drive import Harness, new_run


def envelope(
    state: DynamicStrategyState | None = None,
) -> tuple[OperationRegistry, RunEnvelope[DynamicStrategyState]]:
    codec = dynamic_operation_registry()
    strategy = DynamicStrategy(config=config())
    core = new_run(strategy, Harness(registry=codec, facts=FACTS))
    return codec, RunEnvelope[DynamicStrategyState](
        schema_version=ENVELOPE_SCHEMA_VERSION,
        fence=HostFence(host_id=HostId(root="host"), epoch=1),
        strategy_id=strategy.declaration.strategy_id,
        state_schema=STATE_SCHEMA,
        core=core,
        strategy=state or DynamicStrategyState(),
        event_cursor=EventCursor(sequence=0),
    )


def round_trip(
    codec: OperationRegistry, saved: RunEnvelope[DynamicStrategyState]
) -> RunEnvelope[DynamicStrategyState]:
    return codec.decode_envelope(RunEnvelope[DynamicStrategyState], codec.encode_envelope(saved))
