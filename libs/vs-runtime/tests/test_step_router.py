"""The router gives the loop its one runner: every step the rule issues has a runner, or is declared unserved.

The table is closed against the request union itself, so a new kind of request that no runner
serves fails here, and a router that leaves a kind out cannot be built.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.recovery import COMMAND_KINDS
from vs_runtime.api.executor_step_runner import DECLARED_REPEATS
from vs_runtime.api.step_router import (
    LIVE_REQUEST_KINDS,
    RECOVERY_ONLY_KINDS,
    UNSERVED_STEP_KINDS,
    NoRunnerError,
    StepRouter,
    StepRunnerParts,
    UnroutedKindError,
    build_step_runner,
    request_kinds,
)
from vs_runtime.api.step_runner import Adopted, CancelSignal, Cleared
from vs_runtime.api.testing import fake_run_seed
from vs_sim.api.testing import VirtualClock, run_virtual

if TYPE_CHECKING:
    from vs_core.api.recovery import StepResult

ROLES = ("executors", "turns", "adoption", "operations", "jobs")


@dataclass
class NamedRunner:
    """A runner that says who ran the step."""

    name: str
    ran: list[str] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)

    async def run(
        self,
        step: core.Request,
        tag: core.RequestId,
        attempt: int,
        deadline_at: float,
        cancel: CancelSignal,
    ) -> StepResult:
        del attempt, deadline_at, cancel
        self.ran.append(tag.root)
        raise NotImplementedError(step)

    async def clear(self, step: core.Request, tag: core.RequestId) -> Adopted | Cleared:
        del step
        self.cleared.append(tag.root)
        return Cleared()


def parts(*, jobs: bool = True) -> tuple[StepRunnerParts, dict[str, NamedRunner]]:
    named = {role: NamedRunner(role) for role in ROLES}
    return (
        StepRunnerParts(
            executors=named["executors"],
            turns=named["turns"],
            adoption=named["adoption"],
            operations=named["operations"],
            jobs=named["jobs"] if jobs else None,
        ),
        named,
    )


def test_the_live_kinds_are_every_request_the_union_has_but_the_prove_or_block_ones() -> None:
    """A request kind added to ``Request`` with no route fails here, before it reaches a loop."""
    everything = request_kinds(core.Request)

    assert everything - RECOVERY_ONLY_KINDS == LIVE_REQUEST_KINDS
    assert everything >= RECOVERY_ONLY_KINDS
    assert len(LIVE_REQUEST_KINDS) == 19
    assert set(DECLARED_REPEATS) < LIVE_REQUEST_KINDS


def test_the_router_serves_every_step_core_opens_but_the_two_it_never_opens() -> None:
    """The router's table is core's own classification of what is a step, not a second claim.

    Core keeps the commands of reconciliation (``COMMAND_KINDS``) out of the steps the loop runs;
    every other kind is a step the loop can hand to the router. The steps without a runner are
    exactly the two core opens only to settle an unknown outcome (``UNSERVED_STEP_KINDS``), which
    a step loop run never does; a step added to the union without a runner, or taken out of it, fails here.
    """
    steps = request_kinds(core.Request) - COMMAND_KINDS

    assert COMMAND_KINDS == request_kinds(core.ReconciliationRequest)
    assert steps - LIVE_REQUEST_KINDS == UNSERVED_STEP_KINDS
    assert UNSERVED_STEP_KINDS == {core.InspectTurn, core.InspectOwnedJob}
    assert RECOVERY_ONLY_KINDS == COMMAND_KINDS | UNSERVED_STEP_KINDS


def test_every_live_kind_has_the_runner_of_its_role() -> None:
    built, named = parts()
    router = build_step_runner(built)
    expected = {
        core.DispatchTurn: "turns",
        core.ResumeSessionTurn: "turns",
        core.SubmitMeasurement: "jobs",
        core.AdoptRevision: "adoption",
        core.VerifyAdoption: "adoption",
        **dict.fromkeys(DECLARED_REPEATS, "executors"),
    }

    for kind in LIVE_REQUEST_KINDS - {core.ExecuteRegisteredOperation}:
        assert router.routes[kind] is named[expected[kind]], kind.__name__
    assert router.operations == {
        core.LifecycleClass.QUERY: named["operations"],
        core.LifecycleClass.IDEMPOTENT_WRITE: named["operations"],
        core.LifecycleClass.SESSION_TURN: named["turns"],
        core.LifecycleClass.OWNED_JOB: named["jobs"],
    }


@given(
    left_out=st.sampled_from(
        sorted(LIVE_REQUEST_KINDS - {core.ExecuteRegisteredOperation}, key=lambda k: k.__name__)
    )
)
def test_a_router_that_leaves_any_live_kind_out_is_refused_naming_it(
    left_out: type[core.Request],
) -> None:
    built, _ = parts()
    full = build_step_runner(built)
    routes = {kind: runner for kind, runner in full.routes.items() if kind is not left_out}

    with pytest.raises(UnroutedKindError, match=left_out.__name__):
        StepRouter(routes, full.operations)


@pytest.mark.parametrize("lifecycle", list(core.LifecycleClass), ids=lambda cls: cls.value)
def test_a_router_that_leaves_an_operation_lifecycle_out_is_refused_naming_it(
    lifecycle: core.LifecycleClass,
) -> None:
    built, _ = parts()
    full = build_step_runner(built)
    operations = {cls: r for cls, r in full.operations.items() if cls is not lifecycle}

    with pytest.raises(UnroutedKindError, match=lifecycle.value):
        StepRouter(full.routes, operations)


def test_a_route_for_a_kind_the_rule_never_issues_is_refused() -> None:
    built, named = parts()
    full = build_step_runner(built)

    with pytest.raises(UnroutedKindError, match="InspectRequest"):
        StepRouter({**full.routes, core.InspectRequest: named["executors"]}, full.operations)


def test_an_environment_with_no_evaluation_step_declares_its_measurement_kinds_unserved() -> None:
    """SkyPilot and Modal run the evaluation helper inside the agent's turn: no job runner exists."""
    built, _ = parts(jobs=False)
    router = build_step_runner(built)

    with pytest.raises(NoRunnerError, match="SubmitMeasurement"):
        router.runner_for(measurement())
    with pytest.raises(NoRunnerError, match="owned_long_running_job"):
        router.runner_for(operation(core.LifecycleClass.OWNED_JOB))


def measurement() -> core.SubmitMeasurement:
    """A step of the measurement kind; the router reads only its class."""
    return core.SubmitMeasurement.model_construct()


def operation(lifecycle: core.LifecycleClass) -> core.ExecuteRegisteredOperation:
    """A registered operation of ``lifecycle``; the router reads only the class on its schema."""
    registered = SimpleNamespace(schema_ref=SimpleNamespace(lifecycle=lifecycle))
    return core.ExecuteRegisteredOperation.model_construct(operation=registered)


@pytest.mark.parametrize(
    ("lifecycle", "role"),
    [
        (core.LifecycleClass.QUERY, "operations"),
        (core.LifecycleClass.IDEMPOTENT_WRITE, "operations"),
        (core.LifecycleClass.OWNED_JOB, "jobs"),
        (core.LifecycleClass.SESSION_TURN, "turns"),
    ],
    ids=lambda value: getattr(value, "value", value),
)
def test_a_registered_operation_goes_to_the_runner_of_its_lifecycle_for_run_and_clear(
    lifecycle: core.LifecycleClass, role: str
) -> None:
    built, named = parts()
    router = build_step_runner(built)
    seed = fake_run_seed([10.0], retries=1)
    tag = seed.tags[0]
    step = operation(lifecycle)

    async def main() -> None:
        assert isinstance(await router.clear(step, tag), Cleared)
        with pytest.raises(NotImplementedError):
            await router.run(step, tag, 1, 10.0, CancelSignal())

    run_virtual(VirtualClock(), main())

    assert named[role].cleared == [tag.root]
    assert named[role].ran == [tag.root]
    assert not [n for name, n in named.items() if name != role and (n.ran or n.cleared)]
