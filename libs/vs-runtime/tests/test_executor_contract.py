"""The shared executor contract: every registered kind, crash at every boundary, stale host, core."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest
from tests.support.executor_cases import CASES, inspect_dispatch_of, inspect_request_of

from vs_core.api import DispatchTurn, ObservationStatus, ResumeSessionTurn
from vs_runtime.api.core import (
    ExecutionResult,
    ReceiptStore,
    SealedExecution,
    Settled,
    receipt_executor_kinds,
    result_type_name,
    settle,
)
from vs_runtime.api.executor_contexts import RevocableLease
from vs_runtime.api.executor_contracts import ProcessKilledError
from vs_runtime.api.observation_contracts import assert_core_accepts

if TYPE_CHECKING:
    from vs_core.api import Observation, RequestBase
    from vs_runtime.api.executor_contracts import ExecutorCase, Scenario

pytestmark = pytest.mark.asyncio

PARAMS = [
    pytest.param(case, scenario, id=f"{case.name}-{scenario.name}")
    for case in CASES
    for scenario in case.scenarios
]


def test_registered_kinds_are_exactly_the_receipt_backed_executor_kinds() -> None:
    registered = [scenario.kind for case in CASES for scenario in case.scenarios]
    assert len(registered) == len(set(registered)), "a kind is registered twice"
    assert set(registered) == receipt_executor_kinds()


@pytest.mark.parametrize(("case", "scenario"), PARAMS)
async def test_a_stale_host_performs_no_effect_and_core_accepts_the_retry(
    case: ExecutorCase, scenario: Scenario
) -> None:
    async with case.world() as world:
        request = await world.prepare(scenario)
        before = world.effects()
        lost = RevocableLease()
        lost.valid = False
        stale = await world.execute(request, lease=lost, crash_at=None)
        assert world.effects() == before
        if scenario.effectful:
            assert stale.observation.observation.status is ObservationStatus.UNKNOWN
            assert not stale.observation.observation.terminal
        older = await world.execute(request, lease=RevocableLease(), crash_at=None)
        assert_core_accepts([stale, older], expect_retry=scenario.effectful)


@pytest.mark.parametrize(("case", "scenario"), PARAMS)
async def test_another_payload_under_one_request_identity_is_rejected(
    case: ExecutorCase, scenario: Scenario
) -> None:
    if not scenario.effectful:
        pytest.skip("queries seal nothing, so there is no identity to conflict with")
    async with case.world() as world:
        request = await world.prepare(scenario)
        first = await world.execute(request, lease=RevocableLease(), crash_at=None)
        effects = world.effects()
        other = await world.execute(
            request, lease=RevocableLease(), crash_at=None, digest="another-payload"
        )
        assert other.observation.observation.status is ObservationStatus.REJECTED
        assert world.effects() == effects
        assert_core_accepts([first, other], expect_retry=False)


def _answer(result: ExecutionResult) -> Observation:
    assert result.observation.target is not None, "an inspection must answer with target facts"
    return result.observation.target.observation


@pytest.mark.parametrize(
    ("case", "scenario"),
    [
        pytest.param(case, scenario, id=f"{case.name}-{scenario.name}")
        for case in CASES
        for scenario in case.scenarios
        if scenario.effectful
    ],
)
async def test_inspect_after_completion_reports_the_executed_result(
    case: ExecutorCase, scenario: Scenario
) -> None:
    async with case.world() as world:
        request = await world.prepare(scenario)
        done = await world.execute(request, lease=RevocableLease(), crash_at=None)
        answer = await inspect_request_of(world, request)
        got = _answer(answer)
        want = done.observation.observation
        assert (got.status, got.terminal, got.accepted, got.released, got.request_id) == (
            want.status,
            want.terminal,
            want.accepted,
            want.released,
            want.request_id,
        )
        assert_core_accepts([done, answer], expect_retry=False)


def request_key(request: RequestBase) -> str:
    assert request.request_id is not None
    return request.request_id.root


@pytest.mark.parametrize(("case", "scenario"), PARAMS)
async def test_crash_at_every_durable_boundary_recovers_once_and_core_accepts(
    case: ExecutorCase, scenario: Scenario
) -> None:
    """Kill the process at each durable boundary, then restart over the same disk.

    One sweep checks every crash property, because each crash point costs a whole
    world: the effect happens exactly once and core accepts every observation it
    gets (recovery, replay and inspection alike); "never started" (REJECTED) implies
    zero effects (S2); a result that ``run_once`` seals is replayed forever, so it is
    never one to revisit; and a turn's inspection answers with the identity core gave
    the dispatching request, killed at every boundary and not killed at all.
    """
    async with case.world() as probe:
        request = await probe.prepare(scenario)
        before = probe.effects()
        want = await probe.execute(request, lease=RevocableLease(), crash_at=None)
        boundaries = 2 * probe.writes()
        wanted_effects = probe.effects() - before
    dispatching = scenario.kind in (DispatchTurn, ResumeSessionTurn)
    crash_points: list[int | None] = list(range(boundaries))
    if dispatching:
        crash_points.append(None)
    for crash_at in crash_points:
        async with case.world() as world:
            request = await world.prepare(scenario)
            before = world.effects()
            with contextlib.suppress(ProcessKilledError):
                await world.execute(request, lease=RevocableLease(), crash_at=crash_at)
            if isinstance(request, (DispatchTurn, ResumeSessionTurn)):
                dispatched = await inspect_dispatch_of(world, request)
                assert _answer(dispatched).request_id == request.request_id, f"boundary {crash_at}"
                assert_core_accepts([dispatched], expect_retry=False)
            answer = await inspect_request_of(world, request)
            if _answer(answer).status is ObservationStatus.REJECTED:
                assert world.effects() == before, f"boundary {crash_at}"
            recovered = await world.execute(request, lease=RevocableLease(), crash_at=None)
            replayed = await world.execute(request, lease=RevocableLease(), crash_at=None)
            after = await inspect_request_of(world, request)
            got = recovered.observation.observation
            assert got.request_id == want.observation.observation.request_id
            done = got.status is want.observation.observation.status
            assert world.effects() - before <= wanted_effects, f"boundary {crash_at}"
            if done:
                assert world.effects() - before == wanted_effects, f"boundary {crash_at}"
            assert_core_accepts([recovered, replayed], expect_retry=False)
            assert_core_accepts([answer, recovered, after], expect_retry=False)
            history = ReceiptStore(world.receipts_namespace()).history(request_key(request))
            if isinstance(history, SealedExecution) and history.result_type == result_type_name(
                ExecutionResult
            ):
                sealed = ExecutionResult.model_validate_json(history.result_json)
                assert isinstance(settle(sealed), Settled), f"boundary {crash_at}"
