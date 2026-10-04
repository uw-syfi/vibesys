"""The shared executor contract: every registered kind, crash at every boundary, stale host, core."""

from __future__ import annotations

import contextlib

import pytest
from tests.support.executor_cases import CASES
from tests.support.executor_context import RevocableLease
from tests.support.executor_harness import ProcessKilledError
from tests.support.observation_contract import assert_core_accepts

from vs_core.api import ObservationStatus
from vs_runtime.api.core import receipt_executor_kinds

pytestmark = pytest.mark.asyncio

PARAMS = [
    pytest.param(case, scenario, id=f"{case.name}-{scenario.name}")
    for case in CASES
    for scenario in case.scenarios
]


@pytest.mark.xfail(strict=True, reason="the workspace executors are not registered yet")
def test_registered_kinds_are_exactly_the_receipt_backed_executor_kinds() -> None:
    registered = [scenario.kind for case in CASES for scenario in case.scenarios]
    assert len(registered) == len(set(registered)), "a kind is registered twice"
    assert set(registered) == receipt_executor_kinds()


@pytest.mark.parametrize(("case", "scenario"), PARAMS)
async def test_crash_at_every_write_boundary_recovers_once_and_core_accepts(case, scenario) -> None:  # noqa: ANN001
    async with case.world() as probe:
        request = await probe.prepare(scenario)
        before = probe.effects()
        want = await probe.execute(request, lease=RevocableLease(), crash_at=None)
        boundaries = 2 * probe.writes()
        wanted_effects = probe.effects() - before
    for crash_at in range(boundaries):
        async with case.world() as world:
            request = await world.prepare(scenario)
            before = world.effects()
            with contextlib.suppress(ProcessKilledError):
                await world.execute(request, lease=RevocableLease(), crash_at=crash_at)
            recovered = await world.execute(request, lease=RevocableLease(), crash_at=None)
            replayed = await world.execute(request, lease=RevocableLease(), crash_at=None)
            got = recovered.observation.observation
            assert got.request_id == want.observation.observation.request_id
            done = got.status is want.observation.observation.status
            assert world.effects() - before <= wanted_effects, f"boundary {crash_at}"
            if done:
                assert world.effects() - before == wanted_effects, f"boundary {crash_at}"
            assert_core_accepts([recovered, replayed], expect_retry=False)


@pytest.mark.parametrize(("case", "scenario"), PARAMS)
async def test_a_stale_host_performs_no_effect_and_core_accepts_the_retry(case, scenario) -> None:  # noqa: ANN001
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
async def test_another_payload_under_one_request_identity_is_rejected(case, scenario) -> None:  # noqa: ANN001
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
