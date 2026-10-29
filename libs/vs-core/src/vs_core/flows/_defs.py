"""Flow registration (``@flow``) and the bookkeeping of one running flow."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter

from vs_core._registry import ContractError

from ._sources import First, Pending, Signal, StopSource, Task

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from types import UnionType

    from ._flow import Group
    from ._spans import SpanId
    from ._state import Held

UNBOUNDED = 1_000_000
"""``Group.free`` of a group without a limit."""

FLOW_ATTRIBUTE = "__flow_def__"


@dataclass(frozen=True)
class FlowDef:
    """A flow function registered by name, with the type of the snapshot it restarts from."""

    name: str
    fn: Callable[..., Coroutine[Any, Any, Any]]
    snapshot: type[Any] | UnionType
    """The snapshot's type, or a union of its versions (each a model the others never decode)."""

    @property
    def snapshot_name(self) -> str:
        """The snapshot type as an error message names it."""
        return getattr(self.snapshot, "__name__", None) or str(self.snapshot)


def flow[F: Callable[..., Coroutine[Any, Any, Any]]](
    *, snapshot: type[Any] | UnionType, name: str | None = None
) -> Callable[[F], F]:
    """Register a flow function that takes a ``snapshot`` as its argument and can restart from one.

    ``group.start(key, fn, snapshot)`` starts it from that snapshot; ``ctx.checkpoint(s)``
    records a newer one. ``snapshot`` may be a union of models (``Start | Resume``): a flow
    started from one and checkpointed at a later point as another.
    """

    def register(fn: F) -> F:
        setattr(fn, FLOW_ATTRIBUTE, FlowDef(name or getattr(fn, "__name__", "flow"), fn, snapshot))
        return fn

    return register


def definition_of(fn: Callable[..., Coroutine[Any, Any, Any]]) -> FlowDef | None:
    """The registration of ``fn``, if it has one."""
    return getattr(fn, FLOW_ATTRIBUTE, None)


@cache
def _adapter[T](model: type[T]) -> TypeAdapter[T]:
    return TypeAdapter(model)


@cache
def snapshot_adapter(definition: FlowDef) -> TypeAdapter[Any]:
    """The codec of ``definition``'s snapshots: one type, or a union of its versions."""
    return TypeAdapter(definition.snapshot)


@dataclass(eq=False)
class _Task:
    """One running flow: the root or a task of a group."""

    path: str
    group: Group[Any] | None = None
    coro: Coroutine[Any, Any, Any] | None = None
    blocked: First | None = None
    flow: FlowDef | None = None
    snapshot: object | None = None
    """The latest snapshot of a registered flow: what a restart resumes it from."""
    checkpointed: bool = False
    holds: list[Held] = field(default_factory=list)
    """Workspaces this flow has open (acquired, not yet settled)."""
    carried: list[Held] = field(default_factory=list)
    """Workspaces a restart kept open for this resumed flow, not yet reopened by it."""
    shield: int = 0
    stop_pending: bool = False
    stopped: bool = False
    stop: StopSource = field(default_factory=StopSource)
    handle: Task[Any] = field(default_factory=lambda: Task("root"))
    started: list[Pending] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    span: SpanId | None = None
    """The task's span in the run trace, from the moment it starts."""
    last_operation: SpanId | None = None
    """The span of the operation the task issued last."""

    @property
    def span_id(self) -> SpanId:
        """The task's span; a task has one from the moment it starts."""
        if self.span is None:
            raise ContractError(("flow",), "a task has its span once it starts")
        return self.span
