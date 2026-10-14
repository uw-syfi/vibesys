"""The shared executor contract: every registered kind, crash at every boundary, stale host, core."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest
from tests.support.executor_cases import CASES, inspect_dispatch_of, inspect_request_of

from vs_core.api import DispatchTurn, ObservationStatus, ResumeSessionTurn
from vs_runtime.api.core import (
    ExecutionResult,
    NeverBegun,
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
from vs_sim.api.testing import swept

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


def _answer(result: ExecutionResult) -> Observation:
    assert result.observation.target is not None, "an inspection must answer with target facts"
    return result.observation.target.observation


def _holds_nothing(observation: Observation) -> bool:
    """A "never started" answer proves release: core ends the owner without inspecting again."""
    return observation.released and observation.children_complete and not observation.accepted


@pytest.mark.parametrize(("case", "scenario"), PARAMS)
async def test_a_kind_acts_once_under_its_lease_and_its_identity_and_core_accepts_every_answer(
    case: ExecutorCase, scenario: Scenario
) -> None:
    """One world per kind walks the effect's life, because each step needs the one before.

    A host that lost its lease does nothing and core accepts the retry. The current host then
    acts and leaves an execution record. An inspection reports what was executed. Another payload
    under the same request identity is rejected and changes nothing.
    """
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
        done = await world.execute(request, lease=RevocableLease(), crash_at=None)
        assert_core_accepts([stale, done], expect_retry=scenario.effectful)
        if not scenario.effectful:
            return  # queries seal nothing, so there is no record or identity to conflict with
        assert request.request_id is not None
        history = ReceiptStore(world.receipts_namespace()).history(request.request_id.root)
        assert not isinstance(history, NeverBegun)
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
        effects = world.effects()
        other = await world.execute(
            request, lease=RevocableLease(), crash_at=None, digest="another-payload"
        )
        assert other.observation.observation.status is ObservationStatus.REJECTED
        assert world.effects() == effects
        assert_core_accepts([done, answer], expect_retry=False)
        assert_core_accepts([done, other], expect_retry=False)


# The workspace executor rejects a request on a never-ensured workspace without proving
# release. Claiming it needs a fence first: an EnsureWorkspace still in flight could bind the
# workspace after the answer (the session lifecycle fences CancelTurn this way). Strict, so
# fixing one fails here until its entry is removed.
_HOLDING_UNPROVEN = frozenset(
    {"ensure", "restore", "snapshot", "retain", "discard", "adopt", "verify"}
)
NEVER_SEEDED = [
    pytest.param(
        case,
        scenario,
        id=f"{case.name}-{scenario.name}",
        marks=pytest.mark.xfail(strict=True, reason="workspace rejections prove no release")
        if case.name == "workspaces" and scenario.name in _HOLDING_UNPROVEN
        else (),
    )
    for case in CASES
    for scenario in case.scenarios
]


@pytest.mark.parametrize(("case", "scenario"), NEVER_SEEDED)
async def test_a_terminal_rejection_of_a_request_whose_referent_never_existed_holds_nothing(
    case: ExecutorCase, scenario: Scenario
) -> None:
    """A request whose session, job or resource was never made in this world holds nothing.

    The request is built in one world and run in a fresh one, so whatever it refers to was
    never seeded. Core ends an owning record only on a proven release, so a terminal rejection
    there must prove release; a bare one strands the record for ever.
    """
    async with case.world() as seeded:
        request = await seeded.prepare(scenario)
    async with case.world() as empty:
        answer = (await empty.execute(request, lease=RevocableLease(), crash_at=None)).observation
        observed = answer.observation
        if observed.status is ObservationStatus.REJECTED and observed.terminal:
            assert _holds_nothing(observed), observed.diagnostic


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
    crash_points: list[int | None] = list(swept(boundaries, 2))
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
                if _answer(dispatched).status is ObservationStatus.REJECTED:
                    assert _holds_nothing(_answer(dispatched)), f"boundary {crash_at}"
                assert_core_accepts([dispatched], expect_retry=False)
            answer = await inspect_request_of(world, request)
            if _answer(answer).status is ObservationStatus.REJECTED:
                assert world.effects() == before, f"boundary {crash_at}"
                assert _holds_nothing(_answer(answer)), f"boundary {crash_at}"
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
