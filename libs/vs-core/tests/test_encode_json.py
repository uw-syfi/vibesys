"""`encode_json` writes the bytes pydantic writes, whatever it remembers from earlier calls."""

from __future__ import annotations

from contextlib import contextmanager
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from hypothesis import given
from hypothesis import strategies as st
from pydantic import field_serializer

from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    EventCursor,
    RunEnvelope,
    StrategyState,
    Value,
    encode_json,
    initial_state,
    verify_encoding,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


class Color(StrEnum):
    RED = "red"
    BLUE = "blue"


class Leaf(Value):
    """A value with every kind of leaf the wire has: floats, bytes, enums, optional fields."""

    ratio: float
    blob: bytes = b""
    color: Color = Color.RED
    note: str | None = None
    members: frozenset[str] = frozenset()


class Tree(StrategyState):
    """A strategy state that nests to any depth and carries leaves anywhere."""

    label: str = ""
    leaf: Leaf | None = None
    children: tuple[Tree, ...] = ()


_TOKEN_LOOKALIKES = ("\x00F0\x00", "\x00F1\x00", '"\x00F2\x00"', "\\u0000F3\\u0000", "\x00F99\x00")
_texts = st.one_of(st.text(max_size=6), st.sampled_from(_TOKEN_LOOKALIKES))
_leaves = st.builds(
    Leaf,
    ratio=st.floats(),  # includes nan, inf and tiny and huge magnitudes
    blob=st.text(max_size=4).map(str.encode),
    color=st.sampled_from(Color),
    note=st.none() | _texts,
    members=st.frozensets(_texts, max_size=3),
)
_trees: st.SearchStrategy[Tree] = st.recursive(
    st.builds(Tree, schema_version=st.just(1), label=_texts, leaf=st.none() | _leaves),
    lambda children: st.builds(
        Tree,
        schema_version=st.just(1),
        label=_texts,
        leaf=st.none() | _leaves,
        children=st.lists(children, max_size=3).map(tuple),
    ),
    max_leaves=10,
)


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


@contextmanager
def _verifying() -> Iterator[None]:
    """Make `encode_json` check itself against the plain encoding, as the byte-identity tests do."""
    verify_encoding(enabled=True)
    try:
        yield
    finally:
        verify_encoding(enabled=False)


@given(tree=_trees)
def test_the_encoding_equals_the_plain_encoding_first_and_when_remembered(tree: Tree) -> None:
    envelope = _envelope(tree)
    plain = envelope.model_dump_json().encode()
    with _verifying():
        assert encode_json(envelope) == plain
        assert encode_json(envelope) == plain  # every sub-tree now comes from memory
        assert encode_json(_envelope(tree)) == plain  # an equal envelope sharing its sub-trees


@given(first=_trees, second=_trees, label=_texts)
def test_a_successor_that_shares_sub_trees_encodes_like_a_fresh_value(
    first: Tree, second: Tree, label: str
) -> None:
    successor = first.model_copy(update={"label": label, "children": (second, *first.children)})
    envelope = _envelope(successor)
    with _verifying():
        encode_json(_envelope(first))
        assert encode_json(envelope) == envelope.model_dump_json().encode()


def test_a_string_that_spells_a_splice_token_is_written_as_the_string() -> None:
    twin = Tree(schema_version=1, label="\x00F1\x00")
    tree = Tree(schema_version=1, label="outer", children=(twin, Tree(schema_version=1)))
    with _verifying():
        for _ in range(2):
            envelope = _envelope(tree)
            assert encode_json(envelope) == envelope.model_dump_json().encode()


def _counting_values() -> tuple[Any, Any, list[Any]]:
    """A value class that records each serialization of its instances, a holder of them, the record."""
    writes: list[Any] = []

    class Counted(Value):
        n: int = 0

        @field_serializer("n")
        def _count(self, value: int) -> int:
            writes.append(self)
            return value

    class Holder(Value):
        items: tuple[Counted, ...]

    return Counted, Holder, writes


@given(size=st.integers(min_value=1, max_value=8))
def test_only_values_new_since_the_last_encoding_are_serialized(size: int) -> None:
    counted, holder_of, writes = _counting_values()
    items = tuple(counted(n=index) for index in range(size))
    encode_json(holder_of(items=items))
    assert len(writes) == size
    writes.clear()
    extra = counted(n=size)
    holder = holder_of(items=(*items, extra))
    encoded = encode_json(holder)
    assert writes == [extra]
    assert encoded == holder.model_dump_json().encode()


def test_a_value_that_holds_a_mutable_container_is_never_served_from_memory() -> None:
    counted, holder_of, _ = _counting_values()
    items = [counted(n=1)]
    holder = holder_of.model_construct(items=items)  # skips validation, so the list gets in
    assert encode_json(holder) == holder.model_dump_json().encode()
    items.append(counted(n=2))  # the "frozen" value changes under the memory's feet
    assert encode_json(holder) == holder.model_dump_json().encode()


@given(extra=st.integers(), repeat=st.integers(min_value=1, max_value=3))
def test_a_subclass_instance_put_in_a_base_typed_field_by_model_copy_is_written_as_pydantic_does(
    extra: int, repeat: int
) -> None:
    class Base(Value):
        a: int = 0

    class Wide(Base):
        b: int = 1

    class Plain(Value):
        item: Base  # declared as the base, so a Wide in it is written without `b`

    class Own(Value):
        item: Wide

    wide = Wide(a=extra)
    plain = Plain(item=Base()).model_copy(update={"item": wide})  # skips validation
    own = Own(item=wide)
    with _verifying():
        for _ in range(repeat):  # the same instance under both declarations, remembered or not
            assert encode_json(plain) == plain.model_dump_json().encode()
            assert encode_json(own) == own.model_dump_json().encode()
    assert encode_json(plain) != encode_json(own)
