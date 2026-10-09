"""Run publication retains independent child-source ownership and descendants."""

from hypothesis import example, given
from hypothesis import strategies as st

import vs_core.api as core

from .proof_digest import inspect_source
from .test_proof_ownership_regressions import stopped


def observation(
    scope: core.Scope,
    source: str,
    *,
    children: tuple[core.ResourceId, ...] = (),
) -> core.Observation:
    return core.Observation(
        event_id=core.EventId(root=f"observed:{source}"),
        request_id=core.RequestId(root=source),
        scope=scope,
        sequence=1,
        observed_at=1.0,
        status=core.ObservationStatus.SUCCEEDED,
        resource_id=core.ResourceId(root="child"),
        accepted=True,
        terminal=True,
        released=True,
        children_complete=True,
        children=children,
    )


def close_with_sources(
    observations: tuple[core.Observation, ...], *, selected: int
) -> core.Transition:
    state = stopped(core.initial_state())
    lease = core.ChildLease(
        resource_id=core.ResourceId(root="child"),
        scope=observations[0].scope,
        source_requests=tuple(row.request_id for row in observations),
        observation=observations[selected],
        observation_watermarks=tuple(
            core.ChildObservationWatermark(source_request=row.request_id, observation=row)
            for row in observations
        ),
        watermark_history_complete=True,
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "status": core.RunStatus.CLOSING,
                    "result": core.RunResultProposal(outcome="cancelled", reason="cleanup"),
                }
            ),
            "intents": state.intents.model_copy(
                update={
                    "children": (lease,),
                    "intents": tuple(
                        inspect_source(row.request_id, row.scope) for row in observations
                    ),
                }
            ),
        }
    )
    codec = core.OperationRegistry()
    envelope = core.RunEnvelope[core.StrategyState](
        schema_version=core.ENVELOPE_SCHEMA_VERSION,
        fence=core.HostFence(host_id=core.HostId(root="host"), epoch=0),
        strategy_id=state.run.declaration.strategy_id,
        state_schema=state.run.declaration.state_schema,
        core=state,
        strategy=core.StrategyState(schema_version=1),
        event_cursor=core.EventCursor(sequence=0),
    )
    restored = codec.decode_envelope(type(envelope), codec.encode_envelope(envelope)).core
    assert restored == state
    clock = core.ClockAdvanced(now_at=1.0)
    result = core.trace_step(
        restored,
        clock,
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=clock,
                    change=core.SchedulingChange(
                        state=restored.scheduling, signals=(core.RunDrained(),)
                    ),
                ),
            )
        ),
    )
    assert restored == state
    assert result.state.intents.children == (lease,)
    return result


@example(terminal=False, released=False, complete=True, status=core.ObservationStatus.PENDING)
@given(
    terminal=st.booleans(),
    released=st.booleans(),
    complete=st.booleans(),
    status=st.sampled_from(tuple(core.ObservationStatus)),
)
def test_every_retained_source_requires_conclusive_release_before_run_publication(
    *, terminal: bool, released: bool, complete: bool, status: core.ObservationStatus
) -> None:
    state = core.initial_state()
    scope = core.Scope(owner=state.run.run_id, generation=0)
    observations = (
        observation(scope, "a").model_copy(
            update={
                "terminal": terminal,
                "released": released,
                "children_complete": complete,
                "status": status,
            }
        ),
        observation(scope, "b"),
    )
    conclusive = (
        terminal
        and released
        and complete
        and status
        not in (
            core.ObservationStatus.PENDING,
            core.ObservationStatus.UNKNOWN,
        )
    )
    for selected in range(len(observations)):
        result = close_with_sources(observations, selected=selected)
        assert result.state.run.status == (
            core.RunStatus.TERMINAL if conclusive else core.RunStatus.CLOSING
        )
        assert any(isinstance(event, core.RunEnded) for event in result.events) == conclusive


@given(
    source_count=st.integers(min_value=2, max_value=8),
    discovered=st.integers(min_value=0, max_value=7),
)
def test_each_source_descendant_manifest_survives_aggregate_selection(
    source_count: int, discovered: int
) -> None:
    state = core.initial_state()
    scope = core.Scope(owner=state.run.run_id, generation=0)
    manifest_source = discovered % source_count
    observations = tuple(
        observation(
            scope,
            f"source:{index}",
            children=(core.ResourceId(root="unreleased-descendant"),)
            if index == manifest_source
            else (),
        )
        for index in range(source_count)
    )
    for selected in range(source_count):
        result = close_with_sources(observations, selected=selected)
        assert result.state.run.status == core.RunStatus.CLOSING
        assert not any(isinstance(event, core.RunEnded) for event in result.events)
