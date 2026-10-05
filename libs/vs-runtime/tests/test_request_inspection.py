"""``InspectRequest`` answers come from the receipt store's record of the target, for any kind."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel, ConfigDict
from tests.support.executor_context import context_for
from tests.support.runtime_operations import SCOPE

from vs_core.api import InspectRequest, ObservationStatus, RequestId, TargetObservation
from vs_project.api import Project
from vs_runtime.api.core import (
    HAND_ROLLED_ROLES,
    ObservationFactory,
    ReceiptStore,
    RecordedRequestInspector,
    Settled,
    Transient,
    owner_key,
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

    async def answer(self, target: str) -> TargetObservation:
        request = _inspect(target)
        return await self.inspector.answer(request, context_for(request))

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
