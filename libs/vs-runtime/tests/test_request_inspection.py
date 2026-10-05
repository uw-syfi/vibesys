"""``InspectRequest`` answers come from the receipt store's record of the target, for any kind."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel, ConfigDict
from tests.support.executor_context import context_for
from tests.support.runtime_operations import SCOPE

from vs_core.api import (
    EventId,
    InspectRequest,
    Observation,
    ObservationStatus,
    RequestId,
    RequestObserved,
    SessionId,
    SessionObserved,
    TargetObservation,
)
from vs_project.api import Project
from vs_runtime.api.core import (
    HAND_ROLLED_ROLES,
    NOT_TARGET_FACTS,
    ExecutionResult,
    Inspected,
    ObservationFactory,
    ReceiptStore,
    RecordedRequestInspector,
    Settled,
    Transient,
    as_target,
    owner_key,
    settle,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.asyncio


class _Unregistered(BaseModel):
    """A sealed result type no translator or probe knows."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    value: int


def _inspect(target: str) -> InspectRequest:
    return InspectRequest(
        request_id=RequestId(root=f"inspect-{target}"),
        scope=SCOPE,
        deadline_at=100.0,
        target=RequestId(root=target),
        resource_id=None,
    )


class _Setup:
    def __init__(self, base: Path) -> None:
        self.store = ReceiptStore(Project.open(base).state.state_store_namespace("run"))
        self.inspector = RecordedRequestInspector(self.store, ObservationFactory(self.store))

    async def inspect(self, target: str) -> Inspected:
        request = _inspect(target)
        return await self.inspector.answer(request, context_for(request))

    async def answer(self, target: str) -> TargetObservation:
        return (await self.inspect(target)).target

    async def seal(self, target: str, result: ExecutionResult) -> None:
        request = _inspect(target)

        async def perform(
            *, resumed: bool
        ) -> Settled[ExecutionResult] | Transient[ExecutionResult]:
            del resumed
            return settle(result)

        await self.store.run_once(
            target,
            owner=owner_key(request),
            context=context_for(request),
            result_type=ExecutionResult,
            perform=perform,
        )

    async def run(self, target: str, *, sealed: bool) -> None:
        request = _inspect(target)  # any request: only its fence and digest matter here
        context = context_for(request)

        async def perform(*, resumed: bool) -> Settled[_Unregistered] | Transient[_Unregistered]:
            del resumed
            result = _Unregistered(value=1)
            return Settled(result) if sealed else Transient(result)

        await self.store.run_once(
            target,
            owner=owner_key(request),
            context=context,
            result_type=_Unregistered,
            perform=perform,
        )


@pytest.fixture
def setup() -> Iterator[_Setup]:
    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw)
        yield _Setup(base)


async def test_a_request_with_no_record_is_never_started_only_when_every_role_uses_run_once(
    setup: _Setup,
) -> None:
    status = (await setup.answer("nobody")).observation.status
    expected = ObservationStatus.UNKNOWN if HAND_ROLLED_ROLES else ObservationStatus.REJECTED
    assert status is expected


async def test_a_begun_request_without_a_result_is_unknown_and_not_terminal(
    setup: _Setup,
) -> None:
    await setup.run("begun", sealed=False)
    answer = (await setup.answer("begun")).observation
    assert answer.status is ObservationStatus.UNKNOWN
    assert not answer.terminal


async def test_a_sealed_result_of_a_type_without_a_translation_is_unknown_naming_the_type(
    setup: _Setup,
) -> None:
    await setup.run("sealed", sealed=True)
    answer = (await setup.answer("sealed")).observation
    assert answer.status is ObservationStatus.UNKNOWN
    assert "_Unregistered" in answer.diagnostic


def _observation(target: str) -> Observation:
    return Observation(
        event_id=EventId(root=f"{target}:observation"),
        request_id=RequestId(root=target),
        scope=SCOPE,
        sequence=0,
        observed_at=1.0,
        status=ObservationStatus.SUCCEEDED,
        accepted=True,
        terminal=True,
        released=True,
        children_complete=True,
    )


async def test_a_sealed_result_is_replayed_whole_with_its_owner_events(setup: _Setup) -> None:
    observation = _observation("sealed-whole")
    owner = SessionObserved(session_id=SessionId(root="s"), observation=observation)
    sealed = ExecutionResult(
        observation=RequestObserved(observation=observation), owner_events=(owner,)
    )
    await setup.seal("sealed-whole", sealed)
    inspected = await setup.inspect("sealed-whole")
    assert inspected.owner_events == sealed.owner_events
    assert inspected.target == as_target(sealed.observation)


def test_every_field_of_an_observation_reaches_its_inspection_or_is_reviewed_as_omitted() -> None:
    """A field added to ``RequestObserved`` cannot be silently dropped by inspection."""
    carried = set(TargetObservation.model_fields)
    dropped = set(RequestObserved.model_fields) - carried
    assert dropped == set(NOT_TARGET_FACTS) - carried
    assert dropped <= {"kind", "target", "outcome"}, "review the omit list before extending it"


def test_a_projection_copies_every_carried_field_by_value() -> None:
    observation = _observation("projected")
    observed = RequestObserved(
        observation=observation,
        outcome_json='{"answer": 1}',
        evidence=(),
    )
    target = as_target(observed)
    for name in set(RequestObserved.model_fields) - NOT_TARGET_FACTS:
        assert getattr(target, name) == getattr(observed, name), name
