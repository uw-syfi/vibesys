"""Both app logs and the headless renderer report the independent checkout outcome."""

from __future__ import annotations

from io import StringIO

from hypothesis import given
from hypothesis import strategies as st
from tests.support.real_git import NO_GIT, NO_GITHUB

from headless.render import HeadlessRenderer
from vibesys.events import AgentOutputChunkData, CoreEvent
from vibesys.run.integration import LocalRunIntegration
from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    Adoption,
    AdoptionPublication,
    CheckoutStatus,
    CheckoutUpdate,
    EventCursor,
    EventId,
    Observation,
    ObservationStatus,
    RequestId,
    RunEnvelope,
    Scope,
    StrategyState,
    TrustedBaseline,
    initial_state,
)
from vs_runtime.api.core import RuntimeRecord
from vs_runtime.api.testing import idle_environment_system
from vs_sim.api.testing import ManualClock, SimThreads


def _record() -> RuntimeRecord[StrategyState]:
    core = initial_state()
    envelope = RunEnvelope[StrategyState](
        schema_version=ENVELOPE_SCHEMA_VERSION,
        strategy_id=core.run.declaration.strategy_id,
        state_schema=core.run.declaration.state_schema,
        core=core,
        strategy=StrategyState(schema_version=1),
        event_cursor=EventCursor(sequence=0),
    )
    return RuntimeRecord.fresh(envelope)


def _published(
    record: RuntimeRecord[StrategyState], status: CheckoutStatus
) -> RuntimeRecord[StrategyState]:
    core = record.envelope.core
    adoption = Adoption(
        selection=TrustedBaseline(revision=core.run.facts.baseline),
        observation=Observation(
            event_id=EventId(root="adopted"),
            request_id=RequestId(root="adopt"),
            scope=Scope(owner=core.run.run_id, generation=0),
            sequence=1,
            observed_at=1.0,
            status=ObservationStatus.SUCCEEDED,
            accepted=True,
            terminal=True,
        ),
        verified=True,
        publication=AdoptionPublication(
            revision=core.run.facts.baseline.revision_id.root,
            commit="a" * 40,
            branch="vibesys-adoptions/run",
        ),
        checkout=CheckoutUpdate(status=status),
    )
    state = core.model_copy(
        update={"settlement": core.settlement.model_copy(update={"adoption": adoption})}
    )
    return record.model_copy(
        update={"envelope": record.envelope.model_copy(update={"core": state})}
    )


@given(status=st.sampled_from(tuple(CheckoutStatus)))
def test_completion_logs_publish_once_and_show_the_checkout_outcome(status: CheckoutStatus) -> None:
    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=NO_GIT,
        github=NO_GITHUB,
        threads=SimThreads(),
        system=idle_environment_system(),
    )
    heard: list[CoreEvent] = []
    integration.events.subscribe(heard.append)
    before = _record()
    after = _published(before, status)
    observer = integration.core_commit_observer("run", None, "core")
    observer.committed(before, after)
    diagnostics = [event for event in heard if isinstance(event.data, AgentOutputChunkData)]
    assert len(diagnostics) == 1
    data = diagnostics[0].data
    assert isinstance(data, AgentOutputChunkData)
    assert data.channel == "diagnostic"
    assert f"published {'a' * 12} on vibesys-adoptions/run" in data.content
    if status is CheckoutStatus.FAST_FORWARDED:
        assert "; checkout updated" in data.content
    else:
        assert "; checkout skipped:" in data.content
        assert status.value.replace("_", " ") in data.content
    observer.committed(after, after)
    integration.core_commit_observer("run", None, "core").committed(after, after)
    assert [event for event in heard if isinstance(event.data, AgentOutputChunkData)] == diagnostics
    terminal = StringIO()
    HeadlessRenderer(out=terminal, color=False).handle(diagnostics[0])
    assert data.content in terminal.getvalue()
    integration.close()
