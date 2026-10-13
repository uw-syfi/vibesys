"""A step returns every sub-tree it leaves equal as the very object it was given.

The durable commit remembers facts per instance (verdicts, JSON text) and so does anything
else keyed by identity: a step that rebuilds an unchanged value makes it a new object, and
everything remembered about it is lost, so the cost of a step grows with the run's history.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

import vs_core.api as core


class Outcome(core.Value):
    status: Literal["succeeded"] = "succeeded"


class Query(core.OperationRequest):
    kind: Literal["test.query"] = "test.query"
    lifecycle: Literal[core.LifecycleClass.QUERY] = core.LifecycleClass.QUERY
    outcome_model: ClassVar[type[BaseModel]] = Outcome
    value: int


def _rebuilt(old: object, new: object, path: str = "state") -> list[str]:
    """Paths where ``new`` equals ``old`` but is another object, comparing tuples by position."""
    if old is new:
        return []
    if isinstance(old, BaseModel) and type(new) is type(old):
        if old == new:
            return [path]
        return [
            found
            for name in type(old).model_fields
            for found in _rebuilt(getattr(old, name), getattr(new, name), f"{path}.{name}")
        ]
    if isinstance(old, tuple) and isinstance(new, tuple):
        return [
            found
            for index, (before, after) in enumerate(zip(old, new, strict=False))
            for found in _rebuilt(before, after, f"{path}[{index}]")
        ]
    return []


def _registry() -> core.OperationRegistry:
    descriptor = core.OperationDescriptor(
        kind="test.query",
        lifecycle=core.LifecycleClass.QUERY,
        request_schema=core.SchemaRef(name="query", version=1),
        outcome_schema=core.SchemaRef(name="outcome", version=1),
    )
    return core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=descriptor, request_model=Query, outcome_model=Outcome
            ),
        )
    )


@given(st.integers(min_value=2, max_value=7))
def test_a_step_returns_what_it_left_unchanged_as_the_same_objects(count: int) -> None:
    codec = _registry()
    state = core.initial_state()
    state = state.model_copy(
        update={
            "registry": codec.descriptors,
            "run": state.run.model_copy(
                update={"capabilities": core.Capabilities(operations=codec.descriptors)}
            ),
        }
    )
    scope = core.Scope(owner=state.run.run_id, generation=0)
    for index in range(count):
        decision = codec.validate_decision(
            core.Operation(
                decision_id=core.DecisionId(root=f"d:{index}"),
                scope=scope,
                request=Query(value=index),
                deadline_at=100.0,
            )
        )
        result = core.step(
            state, core.DecisionSubmitted(decision=decision, expected_revision=state.revision)
        )
        assert _rebuilt(state, result.state) == []
        assert len(result.state.run.receipts) == index + 1
        state = result.state


class Node(core.Value):
    label: str = ""
    children: tuple[Node, ...] = ()


type Shape = tuple[str, tuple[Shape, ...]]

_shapes: st.SearchStrategy[Shape] = st.recursive(
    st.tuples(st.text(max_size=2), st.just(())),
    lambda children: st.tuples(st.text(max_size=2), st.lists(children, max_size=3).map(tuple)),
    max_leaves=12,
)


def _build(shape: Shape) -> Node:
    label, children = shape
    return Node(label=label, children=tuple(_build(child) for child in children))


@given(before=_shapes, after=_shapes)
def test_sharing_gives_an_equal_value_that_reuses_every_equal_part(
    before: Shape, after: Shape
) -> None:
    old, new = _build(before), _build(after)  # equal parts of the two are distinct objects
    shared = core.share_unchanged(old, new)
    assert shared == new
    assert _rebuilt(old, shared) == []
    assert core.share_unchanged(old, _build(before)) is old
