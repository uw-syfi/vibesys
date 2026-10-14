"""A deep copy of a value that cannot change is the value itself, as for a tuple of strings.

A crash sweep copies the whole world outside the host at every commit of a run (`vs_dst`), and
that world holds every event the run has published. Copying those values field by field made each
copy grow with the run, so a sweep over a run of a few hundred commits held gigabytes.
"""

from __future__ import annotations

import copy

from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import Value


class Leaf(Value):
    ratio: float
    note: str | None = None
    members: frozenset[str] = frozenset()


class Node(Value):
    label: str = ""
    leaf: Leaf | None = None
    children: tuple[Node, ...] = ()


class Holder(Value):
    items: tuple[Leaf, ...]


_leaves = st.builds(
    Leaf,
    ratio=st.floats(allow_nan=False),
    note=st.none() | st.text(max_size=4),
    members=st.frozensets(st.text(max_size=3), max_size=3),
)
_nodes: st.SearchStrategy[Node] = st.recursive(
    st.builds(Node, label=st.text(max_size=4), leaf=st.none() | _leaves),
    lambda children: st.builds(
        Node,
        label=st.text(max_size=4),
        leaf=st.none() | _leaves,
        children=st.lists(children, max_size=3).map(tuple),
    ),
    max_leaves=10,
)


@given(node=_nodes)
def test_a_deep_copy_of_an_immutable_value_is_the_value_and_shares_it_inside_a_container(
    node: Node,
) -> None:
    assert copy.deepcopy(node) is node
    world = {"published": [node, node.children]}
    copied = copy.deepcopy(world)
    assert copied["published"] is not world["published"]
    assert copied["published"][0] is node


@given(leaves=st.lists(_leaves, min_size=1, max_size=3))
def test_a_value_that_holds_a_mutable_container_is_copied_apart_from_it(
    leaves: list[Leaf],
) -> None:
    holder = Holder.model_construct(items=leaves)  # skips validation, so the list gets in
    copied = copy.deepcopy(holder)
    assert copied is not holder
    assert copied == holder
    leaves.append(leaves[0])
    assert len(copied.items) == len(leaves) - 1
