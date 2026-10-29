"""Open a replay launch from a closed product plan: epoch first, then the environment.

Product policy declares what replays, never how: the root, its reply schemas,
its registered operation bindings and its fact projectors. This module composes
the lifecycle owners over one environment and names no strategy.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_proto.api import ReplayStore as ReplayRecords
from vs_proto.api import validate
from vs_runtime._replay_operations import (
    RegisteredReplayExecutor,
    ReplayOperationError,
    ReplayOperationHost,
)
from vs_runtime._replay_run import ReplayRunHost, opened_lifetime, started_epoch
from vs_runtime._replay_runtime import ReplayRuntime
from vs_runtime._replay_wiring import LIFECYCLE_REQUESTS, replay_bindings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine
    from contextlib import AbstractAsyncContextManager

    from protobuf import Message

    from vs_core.api.replay import ReplayFlow
    from vs_project.api import ControlInbox
    from vs_proto.api import Record
    from vs_proto.api import replay as pb
    from vs_runtime._operation_catalog import OperationCatalog
    from vs_runtime._replay_operations import ReplayOperationBinding
    from vs_runtime._replay_run import ReplayLifetime
    from vs_runtime._replay_runtime import ReplayEnding, ReplayExecutor, ReplayLaunch
    from vs_runtime._replay_sessions import ReplayAgentReply
    from vs_runtime._replay_store import ReplayStore
    from vs_runtime._replay_wiring import ReplayOwnerServices
    from vs_runtime._writer_reconciliation import ReplayWriterBarrier
    from vs_sim.api import SleepingClock

SETUP_FAILED = "environment_setup_failed"
"""The interruption reason of an epoch whose environment did not open."""


class ReplayProjector(Protocol):
    """A disposable view folded from the committed record prefix; it holds no authority."""

    @property
    def name(self) -> str: ...

    def project(self, records: tuple[Record, ...]) -> None:
        """Rebuild the view from the complete committed prefix."""
        ...


@dataclass(frozen=True)
class ReplayPlan:
    """The closed product policy of one replay run.

    It declares the root, the reply schemas, the registered operation bindings
    and the projector slots. It constructs no driver, and the runtime never
    branches on the strategy that supplied it.
    """

    root: Callable[[ReplayFlow], Coroutine[object, object, object]]
    allowance: pb.ReplayAllowance
    ending: Callable[[object], ReplayEnding]
    """Pure policy mapping from the root value to a resumable epoch ending."""
    replies: tuple[ReplayAgentReply, ...]
    bindings: tuple[ReplayOperationBinding, ...]
    projectors: tuple[ReplayProjector, ...]

    def __post_init__(self) -> None:
        validate(self.allowance)
        _unique("reply schema", [str(reply.schema) for reply in self.replies])
        _unique("operation binding", [binding.operation for binding in self.bindings])
        _unique("projector", [projector.name for projector in self.projectors])
        for binding in self.bindings:
            if binding.request in LIFECYCLE_REQUESTS:
                problem = (
                    f"operation binding {binding.operation} cannot replace lifecycle owner: "
                    f"{binding.request.desc().type_name}"
                )
                raise ReplayOperationError(problem)


def _unique(what: str, names: list[str]) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            problem = f"duplicate replay plan {what}: {name}"
            raise ReplayOperationError(problem)
        seen.add(name)


class ReplayEnvironment(Protocol):
    """An opened run environment: the owners that execute a plan's operations."""

    @property
    def writer_barrier(self) -> ReplayWriterBarrier | None:
        """The backing's writer proof, or None where the environment proves no writer death."""
        ...

    def executor(
        self, plan: ReplayPlan, host: ReplayOperationHost, *, allow_adoption: bool
    ) -> ReplayExecutor:
        """Bind the plan's operations to this environment's owners under ``host``.

        ``allow_adoption=False`` must keep every adoption from restoring the operator root.
        """
        ...


@dataclass(frozen=True)
class ReplayLaunchResources:
    """The shell's inputs to one lifetime: log writer and exclusion, inbox, clock, environment."""

    store: ReplayStore
    clock: SleepingClock
    environment: Callable[[], AbstractAsyncContextManager[ReplayEnvironment]]
    """Open the run environment; called only after the successor epoch is durable."""
    inbox: ControlInbox | None
    control_poll_seconds: float


def _at(now: float) -> Timestamp:
    seconds = int(now)
    return Timestamp(seconds=seconds, nanos=int((now - seconds) * 1_000_000_000))


@dataclass(frozen=True)
class OwnerReplayEnvironment:
    """The production environment: the lifecycle owners plus the plan's registered owners."""

    services: ReplayOwnerServices
    writer_barrier: ReplayWriterBarrier | None
    catalog: OperationCatalog | None
    """The registered owners serving the plan's bindings; required when it has any."""

    def executor(
        self, plan: ReplayPlan, host: ReplayOperationHost, *, allow_adoption: bool
    ) -> ReplayExecutor:
        """Route lifecycle requests to their owners and bound requests to the catalog."""
        operations: dict[type[Message], ReplayExecutor] = {}
        if plan.bindings:
            if self.catalog is None:
                problem = "the run environment serves no catalog for the plan's operation bindings"
                raise ReplayOperationError(problem)
            registered = RegisteredReplayExecutor(
                self.catalog, self.services.receipts, plan.bindings, host=host
            )
            operations = {binding.request: registered for binding in plan.bindings}
        return replay_bindings(
            self.services, host, plan.replies, allow_adoption=allow_adoption, operations=operations
        )


@asynccontextmanager
async def open_replay(
    plan: ReplayPlan, launch: ReplayLaunch, resources: ReplayLaunchResources
) -> AsyncIterator[ReplayLifetime]:
    """Open the one lifetime of ``launch``: commit its epoch, then open the environment.

    The successor epoch is durable before any environment-open await, so a setup
    failure leaves that epoch ended as interrupted and resumable. Every epoch after
    the first is an explicit operator Resume: its owners never adopt into, and so
    never restore, the operator root. Projectors fold the committed prefix when the
    lifetime closes, whatever its outcome.
    """
    clock = resources.clock
    runtime = ReplayRuntime(resources.store, plan.root, plan.allowance)
    async with started_epoch(runtime, launch, clock):
        lifetime: ReplayLifetime | None = None
        try:
            async with resources.environment() as environment:
                executor = environment.executor(
                    plan,
                    ReplayOperationHost(launch.header.run_id, resources.store, clock),
                    allow_adoption=runtime.epoch == 1,
                )
                host = ReplayRunHost(
                    runtime,
                    executor,
                    clock,
                    plan.ending,
                    writer_barrier=environment.writer_barrier,
                    inbox=resources.inbox,
                    control_poll_seconds=resources.control_poll_seconds,
                )
                lifetime = opened_lifetime(host, launch)
                try:
                    yield lifetime
                finally:
                    _project(plan, runtime)
        except BaseException:
            if lifetime is None:
                now = clock.now()
                runtime.interrupt(
                    SETUP_FAILED,
                    "The run environment did not open. Resume to try again.",
                    now=now,
                    at=_at(now),
                )
            raise


def _project(plan: ReplayPlan, runtime: ReplayRuntime) -> None:
    records = tuple(ReplayRecords.from_binary(runtime.archive.encode()).records)
    for projector in plan.projectors:
        projector.project(records)
