"""Shared generated event payloads, respecting declared field bounds."""

from __future__ import annotations

import collections.abc
import math
import types
import typing

from hypothesis import strategies as st
from pydantic import BaseModel, TypeAdapter

from server.events import ChatThreadId
from vibesys.api import SteerDelivery, SteerDeliveryState, SteerMode, WireSpan
from vibesys.events import EventPayload, RunFailure, RunFailureKind, WorkspaceRootProbeData


def _deliveries() -> st.SearchStrategy[SteerDelivery]:
    """Steer steps with exactly the facts each state names (a step with any other is invalid)."""
    text = st.text(min_size=1, max_size=8)
    facts: dict[SteerDeliveryState, dict[str, st.SearchStrategy[object]]] = {
        SteerDeliveryState.QUEUED: {"mode": st.sampled_from(SteerMode)},
        SteerDeliveryState.WITHDRAW_QUEUED: {"withdraws": text},
        SteerDeliveryState.REACHED: {"role": text},
        SteerDeliveryState.REACHED_MID_TURN: {"role": text},
        SteerDeliveryState.IN_FORCE: {"role": text},
        SteerDeliveryState.DROPPED: {"reason": text},
        SteerDeliveryState.ENDED: {"reason": text},
        SteerDeliveryState.REFUSED: {"reason": text},
    }
    return st.sampled_from(SteerDeliveryState).flatmap(
        lambda state: st.builds(
            SteerDelivery,
            state=st.just(state),
            control_id=st.none() | text,
            **facts[state],
        )
    )


_JSON_SCALARS = st.none() | st.booleans() | st.integers() | st.text(max_size=8)
# Thread ids are drawn from the published schema of the id type itself, so they follow every
# rule it states (no trailing dot, no reserved device name), not a copy of some of them.
_THREAD_ID = TypeAdapter(ChatThreadId).json_schema()
_FIELD_VALUES: dict[str, st.SearchStrategy[object]] = {
    "state_json": st.just(
        '{"controlId":"request-a","action":"INTENT_ACTION_PAUSE","step":"INTENT_STEP_APPLIED"}'
    ),
    "thread_id": st.from_regex(_THREAD_ID["pattern"], fullmatch=True).filter(
        lambda value: len(value) <= _THREAD_ID["maxLength"]
    ),
    "value": st.floats(allow_nan=False, allow_infinity=False),
    "delivery": _deliveries(),
    # Bounded small, so a draw stays cheap; its own bound (``ge=0``) is kept for any other field.
    "rounds_finished": st.integers(min_value=0, max_value=99),
    # A span's id has a pattern a type alias carries, which ``st.from_type`` cannot see.
    "span": st.just(
        TypeAdapter(WireSpan).validate_json(
            '{"id": "flow:#1", "parent": null, "type": "flow", "name": "root",'
            ' "status": "running", "started_at": 0.0}'
        )
    ),
}
# Values by a field's type, for types Hypothesis cannot derive. Keyed by type, not by field
# name, so a new field that reuses a name (``failure`` of an input measurement is an enum) is
# still drawn from its own type.
_TYPE_VALUES: dict[object, st.SearchStrategy[object]] = {
    typing.Any: _JSON_SCALARS,
    # Its optional constrained ``adopted_candidate`` trips its length filter on ``None``.
    RunFailure: st.builds(
        RunFailure,
        kind=st.sampled_from(RunFailureKind),
        reason=st.text(max_size=8),
        workstreams_started=st.integers(min_value=0, max_value=9),
        workstream_budget=st.integers(min_value=0, max_value=9),
        candidates_kept=st.integers(min_value=0, max_value=9),
        adopted_candidate=st.none(),
    ),
}


def _root_probes() -> st.SearchStrategy[WorkspaceRootProbeData]:
    """Probes with exactly the facts each state names (a probe with any other is invalid)."""
    seconds = st.floats(min_value=0, max_value=1e6)
    text = st.text(max_size=8)
    return st.sampled_from(["queued", "retrying", "proven", "failed"]).flatmap(
        lambda state: st.builds(
            WorkspaceRootProbeData,
            state=st.just(state),
            job_id=st.text(min_size=1, max_size=8) if state == "queued" else st.none(),
            waited_seconds=seconds if state == "queued" else st.none(),
            pending_reason=st.none() | text,
            queue_timeout_seconds=st.none() | st.floats(min_value=1, max_value=1e6),
            failure=text if state in {"failed", "retrying"} else st.none(),
            attempt=st.integers(min_value=2, max_value=9) if state == "retrying" else st.none(),
            retry_in_seconds=seconds if state == "retrying" else st.none(),
        )
    )


# Payloads whose fields constrain one another, drawn whole: field by field they are invalid.
_PAYLOAD_VALUES: dict[type[BaseModel], st.SearchStrategy[BaseModel]] = {
    WorkspaceRootProbeData: _root_probes(),
}


def payload_models(union: object) -> tuple[type[EventPayload], ...]:
    return typing.get_args(typing.get_args(union)[0])


def _items_of(annotation: object) -> st.SearchStrategy[object] | None:
    """Values of a list, sequence or ``tuple[X, ...]`` annotation, its items drawn by ``_values``."""
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    sequence = origin in (list, collections.abc.Sequence) and len(args) == 1
    homogeneous_tuple = origin is tuple and len(args) == 2 and args[1] is Ellipsis
    if not (sequence or homogeneous_tuple):
        return None
    # A container's items are drawn like a field's: ``st.from_type`` would build a nested
    # model from its signature, with unbounded floats and none of its bounds.
    items = st.lists(_values(args[0], []), max_size=4)
    return items.map(tuple) if origin is tuple else items


def _values(annotation: object, metadata: list[object]) -> st.SearchStrategy[object]:
    """Values of a field typed ``annotation`` that keep its bounds (``metadata``).

    A nested model is drawn like a payload, its own bounds kept, since ``st.from_type`` builds
    a model from its signature and drops them.
    """
    if annotation in _TYPE_VALUES:
        return _TYPE_VALUES[annotation]
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _models(annotation)
    if isinstance(annotation, types.UnionType) or typing.get_origin(annotation) is typing.Union:
        return st.one_of(*(_values(arg, metadata) for arg in typing.get_args(annotation)))
    if (items := _items_of(annotation)) is not None:
        return items
    # ``Annotated`` keeps the field's bounds, which ``st.from_type`` reads; built at runtime, it
    # is no static type, hence the cast.
    bounded = typing.Annotated[annotation, *metadata] if metadata else annotation
    drawn = st.from_type(typing.cast("type[object]", bounded))
    if annotation is float:  # a payload's numbers are finite: JSON has no NaN or infinity
        return drawn.filter(lambda value: math.isfinite(typing.cast("float", value)))
    return drawn


def _models[M: BaseModel](model: type[M]) -> st.SearchStrategy[M]:
    """Instances of ``model`` with every required field generated and the rest defaulted."""
    if model in _PAYLOAD_VALUES:
        return typing.cast("st.SearchStrategy[M]", _PAYLOAD_VALUES[model])
    required = {
        name: _FIELD_VALUES[name]
        if name in _FIELD_VALUES
        else _values(field.annotation, field.metadata)
        for name, field in model.model_fields.items()
        if field.is_required()
    }
    return st.fixed_dictionaries(required).map(model.model_validate)


def payloads(
    model: type[EventPayload], **overrides: st.SearchStrategy[object]
) -> st.SearchStrategy[EventPayload]:
    """Payloads of ``model`` with every required field generated and the rest defaulted."""
    if not overrides:
        return _models(model)
    required = {
        name: overrides[name]
        if name in overrides
        else (
            _FIELD_VALUES[name]
            if name in _FIELD_VALUES
            else _values(field.annotation, field.metadata)
        )
        for name, field in model.model_fields.items()
        if field.is_required() or name in overrides
    }
    return st.fixed_dictionaries(required).map(model.model_validate)


JSON_SCALARS = _JSON_SCALARS
