"""Process and container faults under the durable session executor, over real stream transports.

The session executor journals a turn's intent before the provider is called, so a turn whose
outcome is lost is Unknown and is never dispatched again. These tests break the provider's
long-lived process at every output position of a turn, for every provider agentshim lists in
``stream_provider_names()`` and every ``ProcessFault``, and check what the run is left with:

* the executor answers with a typed observation (it never raises, hangs or reports an
  answer the provider did not give);
* a turn whose outcome is not known is inspected, never replayed: the provider receives each
  prompt at most once;
* every observation is one core accepts, in order.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import agentshim
import pytest
from tests.support.session_world import (
    dispatch_request,
    ensure_request,
    inspect_request,
    turn_output,
)
from tests.support.stream_session_world import ContainerResolver, StreamHost, open_stream_host

from vs_agent.api.testing import answering_with
from vs_core.api import ObservationStatus
from vs_faults.api import Boundary, FaultPlan, FaultRule, ProcessFault
from vs_project.api import Project
from vs_prompts.api import TemplateRenderer
from vs_runtime.api.core import ExecutionResult, ReceiptStore
from vs_runtime.api.observation_contracts import assert_core_accepts

if TYPE_CHECKING:
    from vs_core.api import RequestBase

PROVIDERS = tuple(agentshim.stream_provider_names())
ANSWER = '{"value": 7}'
TERMINAL_OR_UNKNOWN = {
    ObservationStatus.SUCCEEDED,
    ObservationStatus.FAILED,
    ObservationStatus.UNKNOWN,
}


class _World:
    """One Project directory and a stream host; every ``run`` is a host restart over the disk."""

    def __init__(self, base: Path, provider: str, plan: FaultPlan | None = None) -> None:
        (base / "project").mkdir()
        (base / "workspace").mkdir()
        self._project = Project.open(base / "project")
        resolver = ContainerResolver(
            base / "workspace", TemplateRenderer(base / "workspace"), provider=provider
        )
        self.stream: StreamHost = open_stream_host(
            provider, answering_with(provider, lambda: ANSWER), resolver, plan
        )

    def store(self) -> ReceiptStore:
        return ReceiptStore(self._project.state.state_store_namespace("run"))

    async def run(self, request: RequestBase, *, now_at: float | None = None) -> ExecutionResult:
        return await self.stream.host.run(request, self.store(), now_at=now_at)


def _plan(at: int, fault: ProcessFault) -> FaultPlan:
    return FaultPlan(
        seed=1,
        rules=(FaultRule(boundary=Boundary.PROCESS_OUTPUT, target="agent", at=at, fault=fault),),
    )


def _status(result: ExecutionResult) -> ObservationStatus:
    return result.observation.observation.status


async def _clean_lines(provider: str) -> int:
    with tempfile.TemporaryDirectory() as raw:
        world = _World(Path(raw), provider)
        await world.run(ensure_request())
        await world.run(dispatch_request())
        return world.stream.executor.lines


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_a_clean_turn_answers_over_one_long_lived_process(provider: str) -> None:
    with tempfile.TemporaryDirectory() as raw:
        world = _World(Path(raw), provider)
        await world.run(ensure_request())
        first = await world.run(dispatch_request())
        second = await world.run(dispatch_request("req-two", "inv-2"))

        assert _status(first) is _status(second) is ObservationStatus.SUCCEEDED
        assert turn_output(first) == '{"value":7}'
        assert world.stream.peers.prompts().__len__() == 2
        assert len(world.stream.executor.injected) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("fault", list(ProcessFault))
async def test_a_process_fault_at_every_position_leaves_a_typed_outcome_and_never_a_replay(
    provider: str, fault: ProcessFault
) -> None:
    lines = await _clean_lines(provider)
    for position in range(1, lines + 1):
        where = f"{provider} {fault.value} at line {position}"
        with tempfile.TemporaryDirectory() as raw:
            world = _World(Path(raw), provider, _plan(position, fault))
            await world.run(ensure_request())
            dispatched = await world.run(dispatch_request())
            assert _status(dispatched) in TERMINAL_OR_UNKNOWN, where
            if _status(dispatched) is ObservationStatus.SUCCEEDED:
                assert turn_output(dispatched) == '{"value":7}', where
            prompts_after_dispatch = len(world.stream.peers.prompts())

            # A late retry and an inspection of the same invocation.
            retried = await world.run(dispatch_request(), now_at=500.0)
            inspected = await world.run(inspect_request(), now_at=500.0)

            assert len(world.stream.peers.prompts()) == prompts_after_dispatch, where
            assert _status(retried) in TERMINAL_OR_UNKNOWN, where
            assert inspected.observation.target is not None, where
            assert_core_accepts([dispatched, retried, inspected], expect_retry=False)
