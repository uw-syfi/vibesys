"""Which runner runs which step: one table from a step's kind to the role that runs it.

The loop hands every step to one ``StepRunner`` and never looks at the step's kind. The router is
that one runner: it looks the kind up in a table and passes ``run`` and ``clear`` to the runner of
the step's role. A step of a registered operation is routed by its lifecycle class, because the
class says what the operation does (read, write by key, own a job, run a turn).

The table is closed. Every request kind the recovery rule can issue (``LIVE_REQUEST_KINDS``) and
every lifecycle class of an operation has a runner or is declared unserved by the run
environment that has none (SkyPilot and Modal have no evaluation step: the agent runs the helper
inside its turn). A router that leaves a kind out is refused when it is built, naming the kind, so
a step can never reach a loop with nobody to run it. The check against the request union is a test.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, get_args, get_origin

from vs_core.api import (
    AdoptRevision,
    DispatchTurn,
    ExecuteRegisteredOperation,
    InspectOwnedJob,
    InspectTurn,
    LifecycleClass,
    ResumeSessionTurn,
    SubmitMeasurement,
    VerifyAdoption,
)
from vs_core.api.recovery import COMMAND_KINDS
from vs_runtime._executor_step_runner import DECLARED_REPEATS

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Mapping

    from vs_core.api import Request, RequestId
    from vs_core.api.recovery import StepResult
    from vs_runtime._step_runner import Adopted, CancelSignal, Cleared, StepRunner

UNSERVED_STEP_KINDS: frozenset[type[Request]] = frozenset({InspectTurn, InspectOwnedJob})
"""The steps of the prove-or-block layer that no runner serves, because no step loop run opens one.

Core opens them to settle an outcome it does not know, and the runners never leave one: a step
with no final report has no result and runs again. A job step's report is its job's last
observation (``finish``), so a run that ends over it asks to inspect nothing (``_born_ended``).
A property over the loop model (``opens_no_unserved_step``) and the DST world runner, which
refuses these kinds, keep it so. 6b deletes the kinds.
"""

RECOVERY_ONLY_KINDS: frozenset[type[Request]] = frozenset({*COMMAND_KINDS, *UNSERVED_STEP_KINDS})
"""The requests of the prove-or-block layer: core's commands of reconciliation, which are not steps
(``COMMAND_KINDS``), and the steps no runner serves (``UNSERVED_STEP_KINDS``)."""

LIVE_REQUEST_KINDS: frozenset[type[Request]] = frozenset(
    {
        *DECLARED_REPEATS,
        DispatchTurn,
        ResumeSessionTurn,
        SubmitMeasurement,
        AdoptRevision,
        VerifyAdoption,
        ExecuteRegisteredOperation,
    }
)
"""Every kind of step the recovery rule issues: the 13 the executor runner serves, two turns, a
measurement, two adoptions and a registered operation (routed by its lifecycle class)."""


class UnroutedKindError(ValueError):
    """A request kind or operation lifecycle has neither a runner nor a declaration that it has none."""

    def __init__(self, names: Iterable[str]) -> None:
        """Name every kind left out."""
        super().__init__(f"no runner, and not declared unserved, for: {', '.join(sorted(names))}")


class NoRunnerError(RuntimeError):
    """A step reached a run environment that declared it has no runner for its kind."""

    def __init__(self, name: str) -> None:
        """Name the kind."""
        super().__init__(f"this run environment has no runner for {name}")


@dataclass(frozen=True)
class StepRunnerParts:
    """The runners of one run, by role, as its composition built them."""

    executors: StepRunner
    """The 13 kinds that create, reset, release, retain or read something on an executor."""
    turns: StepRunner
    """A turn: ``DispatchTurn``, ``ResumeSessionTurn`` and a ``SESSION_TURN`` operation."""
    adoption: StepRunner
    """``AdoptRevision`` and ``VerifyAdoption``."""
    operations: StepRunner
    """A ``QUERY`` or ``IDEMPOTENT_WRITE`` registered operation."""
    jobs: StepRunner | None
    """``SubmitMeasurement`` and an ``OWNED_JOB`` operation; ``None`` where the environment has no
    evaluation step."""


class StepRouter:
    """The one ``StepRunner`` the loop runs: it passes each step to the runner of its kind."""

    def __init__(
        self,
        routes: Mapping[type[Request], StepRunner],
        operations: Mapping[LifecycleClass, StepRunner],
        *,
        unserved: Collection[type[Request] | LifecycleClass] = (),
    ) -> None:
        """Route ``routes`` by kind and registered operations by lifecycle class.

        Raises ``UnroutedKindError`` naming each live kind and lifecycle class that has neither a
        runner nor a place in ``unserved``, and refuses a route for a kind the rule never issues.
        """
        by_kind = LIVE_REQUEST_KINDS - {ExecuteRegisteredOperation}
        stray = set(routes) - by_kind
        if stray:
            raise UnroutedKindError(f"a route for {kind.__name__}" for kind in stray)
        missing = [kind.__name__ for kind in by_kind if kind not in routes and kind not in unserved]
        missing += [
            f"operation {cls.value}"
            for cls in LifecycleClass
            if cls not in operations and cls not in unserved
        ]
        if missing:
            raise UnroutedKindError(missing)
        self._routes = MappingProxyType(dict(routes))
        self._operations = MappingProxyType(dict(operations))

    @property
    def routes(self) -> Mapping[type[Request], StepRunner]:
        """The runner of each kind of step but a registered operation."""
        return self._routes

    @property
    def operations(self) -> Mapping[LifecycleClass, StepRunner]:
        """The runner of a registered operation of each lifecycle class that has one."""
        return self._operations

    def runner_for(self, step: Request) -> StepRunner:
        """The runner of ``step``: by its kind, or for an operation by its lifecycle class."""
        if isinstance(step, ExecuteRegisteredOperation):
            lifecycle = step.operation.schema_ref.lifecycle
            return self._found(self._operations.get(lifecycle), f"operation {lifecycle.value}")
        return self._found(self._routes.get(type(step)), type(step).__name__)

    @staticmethod
    def _found(runner: StepRunner | None, name: str) -> StepRunner:
        if runner is None:
            raise NoRunnerError(name)
        return runner

    async def run(
        self,
        step: Request,
        tag: RequestId,
        attempt: int,
        deadline_at: float,
        cancel: CancelSignal,
    ) -> StepResult:
        """Run the step on the runner of its kind."""
        return await self.runner_for(step).run(step, tag, attempt, deadline_at, cancel)

    async def clear(self, step: Request, tag: RequestId) -> Adopted | Cleared:
        """Clear what an earlier attempt of the step left, on the runner that ran it."""
        return await self.runner_for(step).clear(step, tag)


def build_step_runner(parts: StepRunnerParts) -> StepRouter:
    """The router of a run whose runners are ``parts``: the table of kind to role, written once.

    The environment shows in ``parts``, not here: a run environment with no evaluation step passes
    ``jobs=None`` and its measurement kinds are declared unserved.
    """
    routes: dict[type[Request], StepRunner] = dict.fromkeys(DECLARED_REPEATS, parts.executors)
    routes[DispatchTurn] = routes[ResumeSessionTurn] = parts.turns
    routes[AdoptRevision] = routes[VerifyAdoption] = parts.adoption
    operations: dict[LifecycleClass, StepRunner] = {
        LifecycleClass.QUERY: parts.operations,
        LifecycleClass.IDEMPOTENT_WRITE: parts.operations,
        LifecycleClass.SESSION_TURN: parts.turns,
    }
    unserved: list[type[Request] | LifecycleClass] = []
    if parts.jobs is None:
        unserved += [SubmitMeasurement, LifecycleClass.OWNED_JOB]
    else:
        routes[SubmitMeasurement] = parts.jobs
        operations[LifecycleClass.OWNED_JOB] = parts.jobs
    return StepRouter(routes, operations, unserved=unserved)


def request_kinds(alias: object) -> frozenset[type]:
    """Every class in the request union ``alias`` names, through aliases and ``Annotated``."""
    value = getattr(alias, "__value__", alias)
    if get_origin(value) is Annotated:
        return request_kinds(get_args(value)[0])
    members = get_args(value)
    if not members:
        if not isinstance(value, type):
            message = f"{value!r} is neither a request class nor a union of them"
            raise TypeError(message)
        return frozenset({value})
    return frozenset().union(*(request_kinds(member) for member in members))


__all__ = [
    "LIVE_REQUEST_KINDS",
    "RECOVERY_ONLY_KINDS",
    "UNSERVED_STEP_KINDS",
    "NoRunnerError",
    "StepRouter",
    "StepRunnerParts",
    "UnroutedKindError",
    "build_step_runner",
    "request_kinds",
]
