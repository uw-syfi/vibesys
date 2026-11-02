"""A search that resumes never waits on a measurement decision nobody answers.

A search measures the run's input at its start. Resumed on changed code its flows restart from
their checkpoints, so the root measures the input again: core holds that measurement, answers
the decision from it, and the search goes on, with no second run. A run saved by a host that
accepted such a decision and answered nobody (`Lost`) is answered when it resumes, on the same
code (the journal replays) or on changed code (the flows restart). These tests run a flow on
the real core, with the executors' answers scripted by the test, and restart the host by
round-tripping the stored envelope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import pytest

import vs_core.api as core
from vs_core.api.testing import code_changed, fake_search

from .flow_support import ROLES, Config
from .test_measurement_comparison import ended_steps
from .test_measurements import plan

if TYPE_CHECKING:
    from vs_core.api import CoreState, DecisionId, FlowState, SearchDef

WITHIN = 100.0
FOREVER = 1.0e9


class Seen(core.Value):
    """What the root published the last time its measurement of the input was answered."""

    kind: Literal["seen"] = "seen"
    count: int
    evidence: tuple[str, ...]
    status: str


@fake_search("measuring", Config, ROLES)
async def measuring(ctx: core.Flow, config: Config) -> None:
    """Measure the input, say what came back, and wait: a root that checkpoints its config."""
    ctx.checkpoint(config)
    before = ctx.published(Seen)
    measurement = plan(purpose="baseline", submitted_at=ctx.clock.now())
    answer = await ctx.evaluation.measure(measurement, within=WITHIN)
    if isinstance(answer, core.Failed):
        ctx.publish(Seen(count=-1, evidence=(), status=answer.reason.value))
    else:
        count = 1 if before is None else before.count + 1
        evidence = tuple(item.evidence_id.root for item in answer.evidence)
        ctx.publish(Seen(count=count, evidence=evidence, status=answer.status.value))
    await ctx.first(ctx.timer(FOREVER))


CHANGED = code_changed(measuring)
"""The same root after a change that makes it replay differently: its flows restart."""


@dataclass
class Lost:
    """A host that accepted ``decision`` and answered nobody (a host saved before a decision core
    answered at once was addressed to it): the answer is not told, the receipt stays open."""

    decision: DecisionId

    def __call__(self, state: CoreState, event: core.CoreEvent) -> core.Transition:
        step = core.step(state, event)
        taken = any(
            isinstance(item, core.Accepted) and item.decision_id == self.decision
            for item in step.events
        )
        events = tuple(
            item for item in step.events if not (taken and isinstance(item, core.MeasurementResult))
        )
        receipts = tuple(
            r.model_copy(update={"completion": None}) if r.decision_id == self.decision else r
            for r in step.state.run.receipts
        )
        run = step.state.run.model_copy(update={"receipts": receipts})
        return step.model_copy(
            update={"events": events, "state": step.state.model_copy(update={"run": run})}
        )


@dataclass
class Host:
    """One life of a host: the real core, a flow search, and the executors' world around them."""

    definition: SearchDef[Config]
    core: CoreState
    state: FlowState
    stepper: core.CoreStep = core.step
    held: tuple[core.Decision, ...] = ()
    told: list[core.Event] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.registry = self.definition.registry() or core.OperationRegistry()
        self.driver = self.definition.driver(Config(), self.registry)

    @classmethod
    def started(cls, definition: SearchDef[Config]) -> Host:
        """A run whose root has begun and asked for its measurement."""
        registry = definition.registry() or core.OperationRegistry()
        driver = definition.driver(Config(), registry)
        host = core.SearchHost(step=core.step, registry=registry)
        first = core.begin(driver, Config(), core.initial_state(), host)
        found = cls(definition, first.core, first.state)
        found.held = first.held
        return found

    def _advance(self, events: tuple[core.Event, ...]) -> None:
        shell = core.SearchHost(step=self.stepper, registry=self.registry)
        done = core.advance(
            self.driver, self.state, self.core, shell, core.Inbox(events=events, held=self.held)
        )
        self.state, self.core, self.held = done.state, done.core, done.held
        self.told.extend(events)

    def take(self, step: core.Transition) -> None:
        """Core stepped to ``step``: the search is told what core owes it."""
        self.core = step.state
        self._advance(core.translate(step.state, step.events).events)

    def event(self, event: core.CoreEvent) -> None:
        self.take(self.stepper(self.core, event))

    def run_started(self) -> core.SubmitMeasurement:
        (request,) = self.submissions
        return request

    @property
    def submissions(self) -> list[core.SubmitMeasurement]:
        return [
            row.request
            for row in self.core.records.records
            if isinstance(row.request, core.SubmitMeasurement)
        ]

    def end_run(self, request: core.SubmitMeasurement) -> None:
        """The executor ends ``request``'s job: passed, benchmark reading 100."""
        for step in ended_steps(self.core, request, 100.0):
            self.take(step)

    @property
    def seen(self) -> Seen | None:
        return self.driver.published(self.state, Seen)

    def restarted(
        self,
        definition: SearchDef[Config],
        *,
        stepper: core.CoreStep = core.step,
    ) -> Host:
        """The host that takes over: the stored envelope reloaded, recovery started under a new
        epoch, and the search, in the code ``definition``, woken as the first event after it."""
        stored = core.RunEnvelope[core.FlowState](
            schema_version=core.ENVELOPE_SCHEMA_VERSION,
            strategy_id=self.core.run.declaration.strategy_id,
            state_schema=self.core.run.declaration.state_schema,
            core=self.core,
            strategy=self.state,
            event_cursor=core.EventCursor(sequence=0),
        )
        loaded = self.registry.decode_envelope(
            core.RunEnvelope[core.FlowState], self.registry.encode_envelope(stored)
        )
        host = Host(definition, loaded.core, loaded.strategy, stepper=stepper, held=self.held)
        now = loaded.core.run.now_at + 10.0
        host.event(core.ClockAdvanced(now_at=now + 1.0))
        host._advance((core.TimeReached(time=now + 1.0),))
        return host


def measured_once() -> Host:
    """A run whose root asked for its input's measurement, and the executor ended it."""
    host = Host.started(measuring)
    host.end_run(host.run_started())
    assert host.seen is not None
    assert host.seen.count == 1
    return host


def assert_never_measured_again(host: Host, first: core.SubmitMeasurement) -> None:
    assert host.submissions == [first], "the input is measured once, never again"


@pytest.mark.parametrize("later", ["same", "changed"])
def test_a_resumed_search_asking_for_its_input_again_is_answered_from_the_run_that_took_it(
    later: str,
) -> None:
    """Regression: the repeat was accepted with no request and its answer was addressed to nobody,
    so the resumed root waited on it until its limit passed, and the search never went on."""
    host = measured_once()
    first = host.run_started()
    after_change = host.restarted(CHANGED)  # the root restarts and asks again
    assert after_change.seen is not None
    assert host.seen is not None
    assert after_change.seen.count == 2, "the root went on past its second measurement"
    assert after_change.seen.evidence == host.seen.evidence
    assert_never_measured_again(after_change, first)

    code = CHANGED if later == "same" else code_changed(CHANGED)
    resumed = after_change.restarted(code)
    assert resumed.seen is not None
    assert resumed.seen.count == (2 if later == "same" else 3)
    assert resumed.seen.evidence == host.seen.evidence
    assert_never_measured_again(resumed, first)
