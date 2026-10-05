"""A set of lifecycle abilities is written in one order, however it was built."""

from __future__ import annotations

import json
from typing import get_args

from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import Capabilities
from vs_core.types.common import LifecycleCapability

ABILITIES = list(get_args(LifecycleCapability.__value__))


ORDERINGS = st.lists(st.sampled_from(ABILITIES), unique=True).flatmap(
    lambda chosen: st.tuples(st.just(chosen), st.permutations(chosen))
)


@given(ORDERINGS)
def test_capabilities_json_does_not_depend_on_insertion_order(
    orderings: tuple[list[str], list[str]],
) -> None:
    chosen, shuffled = orderings
    left = Capabilities.model_validate({"lifecycle": chosen}, strict=False)
    right = Capabilities.model_validate({"lifecycle": shuffled}, strict=False)
    assert left.model_dump_json() == right.model_dump_json()
    assert json.loads(left.model_dump_json())["lifecycle"] == sorted(chosen)
    assert Capabilities.model_validate_json(left.model_dump_json()) == left
