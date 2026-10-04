"""Scripted area outputs exercise kernel wiring while leaf reducers are pending.

A trace declares values, not a substitute lifecycle implementation. It is useful
for checking routing, propagation, outbox registration and crash serialization.
Wave 1 behavioral tests must call the real named reducers and step.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from vs_core._registry import ContractError
from vs_core._step import consume
from vs_core.types.attempts import AttemptsState
from vs_core.types.common import Area, Value
from vs_core.types.evaluation import EvaluationState
from vs_core.types.intents import IntentsState
from vs_core.types.kernel import AreaChange, CoreEvent, CoreState, Signal, Transition
from vs_core.types.scheduling import SchedulingState
from vs_core.types.sessions import SessionsState
from vs_core.types.settlement import SettlementState

"""Scripted area outputs exercise kernel wiring while leaf reducers are pending.

A trace declares values, not a substitute lifecycle implementation. It is useful
for checking routing, propagation, outbox registration and crash serialization.
Wave 1 behavioral tests must call the real named reducers and step.
"""


class SchedulingChange(AreaChange[SchedulingState]):
    """Typed scheduling output in a declarative kernel trace."""

    area: Literal[Area.SCHEDULING] = Area.SCHEDULING


class AttemptsChange(AreaChange[AttemptsState]):
    """Typed attempts output in a declarative kernel trace."""

    area: Literal[Area.ATTEMPTS] = Area.ATTEMPTS


class SessionsChange(AreaChange[SessionsState]):
    """Typed sessions output in a declarative kernel trace."""

    area: Literal[Area.SESSIONS] = Area.SESSIONS


class EvaluationChange(AreaChange[EvaluationState]):
    """Typed evaluation output in a declarative kernel trace."""

    area: Literal[Area.EVALUATION] = Area.EVALUATION


class SettlementChange(AreaChange[SettlementState]):
    """Typed settlement output in a declarative kernel trace."""

    area: Literal[Area.SETTLEMENT] = Area.SETTLEMENT


class IntentsChange(AreaChange[IntentsState]):
    """Typed intents output in a declarative kernel trace."""

    area: Literal[Area.INTENTS] = Area.INTENTS


type TraceChange = Annotated[
    SchedulingChange
    | AttemptsChange
    | SessionsChange
    | EvaluationChange
    | SettlementChange
    | IntentsChange,
    Field(discriminator="area"),
]


class TraceFrame(Value):
    """One exact area input and its declared output."""

    signal: Signal
    change: TraceChange


class ReducerTrace(Value):
    """Immutable expected propagation order, with no hidden reducer logic."""

    frames: tuple[TraceFrame, ...]


def trace_step(state: CoreState, event: CoreEvent, trace: ReducerTrace) -> Transition:
    """Drive the production kernel with explicit typed leaf outputs."""
    position = 0

    def dispatch(current: CoreState, signal: Signal) -> AreaChange:
        nonlocal position
        del current
        if position >= len(trace.frames):
            raise ContractError(("trace", position), "unexpected area input")
        frame = trace.frames[position]
        position += 1
        if frame.signal != signal:
            raise ContractError(("trace", position - 1), "area input differs from declared trace")
        return frame.change

    result = consume(state, event, dispatch)
    if position != len(trace.frames):
        raise ContractError(("trace", position), "unused area outputs")
    return result
