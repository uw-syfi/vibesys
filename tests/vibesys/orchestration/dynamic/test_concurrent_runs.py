"""The production shell runs several workstreams' turns at once and the dynamic run still ends the same.

The dynamic flow is driven through the production shell and loop (`scripted_host`). Implementer
turns are held for a drawn number of event-loop yields (no sleeps and no wall clock), so a drawn
delay decides the order they finish in, and equal delays are the same-tick tie. A turn that
fails halts the run, but not before what the other turns returned is committed. An agent tool
call that arrives while a publication awaits is committed and never halts the run. (Overlap up
to the cap, completion order and host death are `tests/system`'s: they do not depend on the
search.)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.dynamic_world import DynamicWorld
from tests.vibesys.orchestration.dynamic._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)

from vs_core.api import (
    ClockAdvanced,
    DispatchTurn,
    RequestPhase,
    RunStatus,
)
from vs_project.api import FakeStateStore
from vs_runtime.api.testing import (
    FakePublicationDelivery,
    HostOptions,
    ScriptedExecutors,
    scripted_host,
)
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel

    from vs_core.api import (
        CoreEvent,
        CoreState,
        OperationRegistry,
        Request,
        SchemaRef,
    )
    from vs_core.testing.liveness import Journal
    from vs_runtime.api.core import (
        ExecutionContext,
        ExecutionResult,
        Publication,
        PublicationAcknowledgement,
        PublicationContext,
        RunLoopConfig,
    )
    from vs_runtime.api.testing import Script

WORKSTREAMS = 2


@dataclass
class Ledger:
    """What the executors saw, in the order it happened."""

    in_flight: int = 0
    most_in_flight: int = 0
    delays: list[int] = field(default_factory=list)
    failing: frozenset[int] = frozenset()
    """Ordinals (start order) of the implementer turns that raise after their delay."""
    started: int = 0
    executed: list[str] = field(default_factory=list)
    returned: list[str] = field(default_factory=list)
    """Implementer turns that finished and handed their observation back."""


class TurnFailedError(RuntimeError):
    """An executor error: the turn's process died."""


class DelayedExecutors(ScriptedExecutors):
    """Implementer turns finish after a drawn number of event-loop yields."""

    def __init__(
        self,
        script: Script,
        core: Callable[[], CoreState],
        registry: OperationRegistry,
        schemas: Mapping[SchemaRef, type[BaseModel]],
        journal: Journal,
    ) -> None:
        super().__init__(script, core, registry, schemas, journal)
        self._ledger = Ledger()

    def holding(self, ledger: Ledger) -> DelayedExecutors:
        """These executors, recording into and held by ``ledger``."""
        self._ledger = ledger
        return self

    async def execute(self, request: Request, context: ExecutionContext) -> ExecutionResult:
        assert request.request_id is not None
        ledger = self._ledger
        ledger.executed.append(request.request_id.root)
        if not _is_implementer_turn(request):
            return await super().execute(request, context)
        ordinal = ledger.started
        ledger.started += 1
        ledger.in_flight += 1
        ledger.most_in_flight = max(ledger.most_in_flight, ledger.in_flight)
        try:
            for _ in range(ledger.delays.pop(0) if ledger.delays else 0):
                await asyncio.sleep(0)
            if ordinal in ledger.failing:
                message = f"implementer turn {ordinal} failed"
                raise TurnFailedError(message)
            result = await super().execute(request, context)
            ledger.returned.append(request.request_id.root)
            return result
        finally:
            ledger.in_flight -= 1


def _is_implementer_turn(request: Request) -> bool:
    return isinstance(request, DispatchTurn) and request.turn.session.role_id.root.endswith(
        "implementer"
    )


def world() -> DynamicWorld:
    """Two workstreams."""
    return DynamicWorld(
        planner=[plan_reply(*(implement(f"h{n}") for n in range(WORKSTREAMS)))],
        implementer=[implemented() for _ in range(5 * WORKSTREAMS)],
        judge=[reviewed() for _ in range(4 * WORKSTREAMS)],
        max_in_flight=WORKSTREAMS,
    )


@dataclass
class Built:
    host: Any
    ledger: Ledger


@dataclass(frozen=True)
class Outside:
    """What the run's host is given besides the script: its disk and its publication ledger."""

    store: FakeStateStore
    delivery: FakePublicationDelivery


def build(
    cap: int | None,
    delays: list[int],
    *,
    failing: frozenset[int] = frozenset(),
    outside: Outside | None = None,
) -> Built:
    """The dynamic flow on the production shell, its implementer turns held by ``delays``."""
    script = world()
    setup = script.setup
    harness = setup.harness
    ledger = Ledger(delays=list(delays), failing=failing)
    host = scripted_host(
        setup.new_launch(),
        script,
        harness,
        setup.schemas,
        HostOptions(
            max_concurrent=1 if cap is None else cap,
            translator=lambda *given: DelayedExecutors(*given).holding(ledger),
        ),
        store=None if outside is None else outside.store,
        delivery=None if outside is None else outside.delivery,
    )
    if cap is None:
        config: RunLoopConfig = host.config
        host.config = replace(config, max_concurrent=None)
    return Built(host, ledger)


def finished(cap: int | None, delays: list[int]) -> tuple[Built, str]:
    built = build(cap, delays)
    run = built.host.run()
    assert run.halted is None
    assert run.core.run.status == RunStatus.TERMINAL
    assert run.core.run.result is not None
    return built, run.core.run.result.model_dump_json()


_BASELINE: dict[str, str] = {}


def baseline() -> str:
    """The result of the serial run: one turn at a time, none held."""
    if not _BASELINE:
        _BASELINE["result"] = finished(1, [])[1]
    return _BASELINE["result"]


def test_without_an_explicit_cap_the_runs_own_parallelism_sets_it() -> None:
    """One source of truth: the run's ``max_parallel`` (the search's ``max_in_flight``)."""
    assert finished(None, [40] * WORKSTREAMS)[0].ledger.most_in_flight == WORKSTREAMS


@settings(max_examples=examples(pr=1, nightly=12), deadline=None, derandomize=True, database=None)
@given(
    delays=st.lists(
        st.integers(min_value=0, max_value=3), min_size=WORKSTREAMS, max_size=WORKSTREAMS
    ),
    failing=st.integers(min_value=0, max_value=WORKSTREAMS - 1),
)
def test_a_failing_turn_keeps_what_the_turns_that_finished_with_it_returned(
    delays: list[int], failing: int
) -> None:
    """A failure halts the run, but an observation another flight returned is committed first."""
    built = build(WORKSTREAMS, delays, failing=frozenset({failing}))

    with pytest.raises(TurnFailedError):
        built.host.run()

    records = {
        item.request_id.root: item for item in built.host.shell.record.envelope.core.records.records
    }
    for request_id in built.ledger.returned:
        assert records[request_id].phase == RequestPhase.COMPLETED, request_id


class AdmittingDelivery(FakePublicationDelivery):
    """Publishing awaits, and an agent tool call arrives meanwhile; the delivery may fail."""

    def __init__(self, store: FakeStateStore, *, fails: bool) -> None:
        super().__init__(store)
        self.shell: Any = None
        self.admitted: list[CoreEvent] = []
        self._fails = fails

    async def publish(
        self, publication: Publication, context: PublicationContext
    ) -> PublicationAcknowledgement:
        assert self.shell is not None
        if not self.admitted:
            for _ in range(2):
                event = ClockAdvanced(now_at=context.now_at)
                self.shell.admit(event, now_at=context.now_at)
                self.admitted.append(event)
            if self._fails:
                message = "delivery is down"
                raise OSError(message)
        return await super().publish(publication, context)


@pytest.mark.parametrize("fails", [True, False])
def test_a_tool_call_admitted_during_a_publish_commits_and_never_halts(*, fails: bool) -> None:
    store = FakeStateStore()
    delivery = AdmittingDelivery(store, fails=fails)
    built = build(2, [1, 1], outside=Outside(store, delivery))
    delivery.shell = built.host.shell

    if fails:
        with pytest.raises(OSError, match="delivery is down"):
            built.host.run()
    else:
        run = built.host.run()
        assert run.halted is None
        assert run.core.run.status == RunStatus.TERMINAL
        assert run.core.run.result is not None
        assert run.core.run.result.model_dump_json() == baseline()
    assert len(delivery.admitted) == 2
