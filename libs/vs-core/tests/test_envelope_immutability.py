"""A mutable value at any depth of an envelope is rejected, however often its siblings passed."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    ContractError,
    EventCursor,
    OperationRegistry,
    RunEnvelope,
    StrategyState,
    initial_state,
)
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from collections.abc import Callable


class Tree(StrategyState):
    """A strategy state that nests to any depth, so a poisoned value can sit anywhere."""

    label: str = ""
    tags: tuple[str, ...] = ()
    children: tuple[Tree, ...] = ()


class Loose(BaseModel):
    """A mutable model, which no durable value may contain."""

    label: str = ""


type Shape = tuple[str, tuple[Shape, ...]]

_shapes: st.SearchStrategy[Shape] = st.recursive(
    st.tuples(st.text(max_size=3), st.just(())),
    lambda children: st.tuples(st.text(max_size=3), st.lists(children, max_size=3).map(tuple)),
    max_leaves=12,
)

# Each poison replaces one field of a node with a mutable value the field's schema forbids.
_POISONS: tuple[tuple[str, Callable[[], object]], ...] = (
    ("tags", lambda: ["a"]),
    ("tags", lambda: {"a"}),
    ("tags", lambda: {"a": 1}),
    ("tags", lambda: ("a", ["b"])),
    ("children", lambda: [Tree(schema_version=1)]),
    ("children", lambda: (Tree(schema_version=1), Loose())),
    (
        "children",
        lambda: (Tree.model_construct(schema_version=1, label="x", tags=["y"], children=()),),
    ),
    ("label", Loose),
)


def _build(shape: Shape) -> Tree:
    label, children = shape
    return Tree(schema_version=1, label=label, children=tuple(_build(child) for child in children))


def _envelope(tree: Tree) -> RunEnvelope[Tree]:
    state = initial_state()
    return RunEnvelope[Tree](
        schema_version=ENVELOPE_SCHEMA_VERSION,
        strategy_id=state.run.declaration.strategy_id,
        state_schema=state.run.declaration.state_schema,
        core=state,
        strategy=tree.model_copy(
            update={"schema_version": state.run.declaration.state_schema.version}
        ),
        event_cursor=EventCursor(sequence=0),
    )


def _replace_at(tree: Tree, path: tuple[int, ...], update: dict[str, object]) -> Tree:
    """Rebuild ``tree`` with ``update`` applied to the node at ``path``, sharing every sibling."""
    if not path:
        return tree.model_copy(update=update)
    head, *rest = path
    children = list(tree.children)
    children[head] = _replace_at(children[head], tuple(rest), update)
    return tree.model_copy(update={"children": tuple(children)})


def _paths(tree: Tree) -> list[tuple[int, ...]]:
    found: list[tuple[int, ...]] = [()]
    for index, child in enumerate(tree.children):
        found.extend((index, *path) for path in _paths(child))
    return found


@given(shape=_shapes, choice=st.data())
def test_a_mutable_value_at_any_depth_is_rejected_after_its_siblings_were_accepted(
    shape: Shape, choice: st.DataObject
) -> None:
    registry = OperationRegistry()
    tree = _build(shape)
    # Accepted first, twice: every sub-tree shared with the poisoned copy has been checked.
    registry.encode_envelope(_envelope(tree))
    registry.encode_envelope(_envelope(tree))
    path = choice.draw(st.sampled_from(_paths(tree)))
    field, poison = choice.draw(st.sampled_from(_POISONS))
    poisoned = _replace_at(tree, path, {field: poison()})
    for _ in range(2):  # a refusal is not a one-off: the verdict is remembered, not skipped
        with pytest.raises(ContractError, match="immutable durable value required"):
            registry.encode_envelope(_envelope(poisoned))
    registry.encode_envelope(_envelope(tree))  # the clean original is still accepted


@settings(max_examples=examples(pr=4, nightly=100))
@given(shape=_shapes)
def test_remembered_encodings_equal_a_fresh_encoding_of_an_equal_envelope(shape: Shape) -> None:
    registry = OperationRegistry()
    tree = _build(shape)
    first = registry.encode_envelope(_envelope(tree))
    again = registry.encode_envelope(_envelope(tree))
    rebuilt = registry.encode_envelope(_envelope(_build(shape)))
    assert first == again == rebuilt
