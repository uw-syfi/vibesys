"""However crashes, retries and inspections interleave, an inspection names core's request."""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import TYPE_CHECKING, cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.executor_cases import CASES, inspect_dispatch_of
from tests.support.executor_context import RevocableLease
from tests.support.executor_harness import ProcessKilledError
from tests.support.observation_contract import assert_core_accepts

from vs_core.api import DispatchTurn, ResumeSessionTurn

if TYPE_CHECKING:
    from tests.support.executor_harness import ExecutorCase, Scenario

    from vs_runtime.api.core import ExecutionResult

_DISPATCHING = [
    pytest.param(case, scenario, id=f"{case.name}-{scenario.name}")
    for case in CASES
    for scenario in case.scenarios
    if scenario.kind in (DispatchTurn, ResumeSessionTurn)
]


async def _boundaries(case: ExecutorCase, scenario: Scenario) -> int:
    async with case.world() as probe:
        request = await probe.prepare(scenario)
        await probe.execute(request, lease=RevocableLease(), crash_at=None)
        return 2 * probe.writes()


_STEPS = 20 if os.environ.get("VIBESYS_FULL_PROPERTIES") == "1" else 4


@pytest.mark.parametrize(("case", "scenario"), _DISPATCHING)
def test_whatever_the_order_of_crashes_retries_and_inspections_every_answer_names_the_request(
    case: ExecutorCase, scenario: Scenario
) -> None:
    boundaries = asyncio.run(_boundaries(case, scenario))
    step = st.one_of(
        st.just("inspect"),
        st.integers(min_value=0, max_value=boundaries - 1),
        st.just("complete"),
    )

    @settings(
        max_examples=_STEPS,
        deadline=None,
        derandomize=True,
        suppress_health_check=list(HealthCheck),
    )
    @given(order=st.lists(step, min_size=1, max_size=5))
    def check(order: list[str | int]) -> None:
        async def run() -> None:
            async with case.world() as world:
                request = await world.prepare(scenario)
                assert isinstance(request, (DispatchTurn, ResumeSessionTurn))
                seen: list[ExecutionResult] = []
                for action in order:
                    if action == "inspect":
                        seen.append(await inspect_dispatch_of(world, request))
                        target = seen[-1].observation.target
                        assert target is not None, order
                        assert target.observation.request_id == request.request_id, order
                        continue
                    crash_at = None if action == "complete" else cast("int", action)
                    with contextlib.suppress(ProcessKilledError):
                        seen.append(
                            await world.execute(request, lease=RevocableLease(), crash_at=crash_at)
                        )
                assert_core_accepts(seen, expect_retry=False)

        asyncio.run(run())

    check()
