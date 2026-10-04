"""Envelope harness: the dynamic registry, declaration and a persisted-state round trip."""

from __future__ import annotations

from vibesys.orchestration.dynamic.strategy.api import (
    STATE_SCHEMA,
    DynamicStrategyState,
    dynamic_operation_registry,
)
from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    Capabilities,
    EventCursor,
    HostFence,
    HostId,
    OperationRegistry,
    OperationSchemaRef,
    RunEnvelope,
    validate_startup,
)
from vs_core.testing.builders import initial_state


def envelope(
    state: DynamicStrategyState | None = None,
) -> tuple[OperationRegistry, RunEnvelope[DynamicStrategyState]]:
    codec = dynamic_operation_registry()
    core = initial_state()
    declaration = core.run.declaration.model_copy(
        update={
            "state_schema": STATE_SCHEMA,
            "required_operations": tuple(
                OperationSchemaRef(
                    kind=item.kind,
                    request_schema=item.request_schema,
                    outcome_schema=item.outcome_schema,
                    lifecycle=item.lifecycle,
                )
                for item in codec.descriptors
            ),
        }
    )
    capabilities = validate_startup(declaration, Capabilities(operations=codec.descriptors))
    core = core.model_copy(
        update={
            "registry": codec.descriptors,
            "run": core.run.model_copy(
                update={"declaration": declaration, "capabilities": capabilities}
            ),
        }
    )
    return codec, RunEnvelope[DynamicStrategyState](
        schema_version=ENVELOPE_SCHEMA_VERSION,
        fence=HostFence(host_id=HostId(root="host"), epoch=1),
        strategy_id=declaration.strategy_id,
        state_schema=STATE_SCHEMA,
        core=core,
        strategy=state or DynamicStrategyState(),
        event_cursor=EventCursor(sequence=0),
    )


def round_trip(
    codec: OperationRegistry, saved: RunEnvelope[DynamicStrategyState]
) -> RunEnvelope[DynamicStrategyState]:
    return codec.decode_envelope(RunEnvelope[DynamicStrategyState], codec.encode_envelope(saved))
