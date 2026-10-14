"""The whole-loop harness resolves a strategy by plugin id and refuses an unknown one by name."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.loop_spec import LOOPS, UnknownLoopError, loop_spec


def test_every_registered_strategy_resolves_to_its_own_spec() -> None:
    assert LOOPS
    assert all(loop_spec(plugin_id) is spec for plugin_id, spec in LOOPS.items())


@given(plugin_id=st.text(max_size=12).filter(lambda text: text not in LOOPS))
def test_an_unknown_plugin_id_is_refused_naming_it_and_the_known_ids(plugin_id: str) -> None:
    with pytest.raises(UnknownLoopError) as raised:
        loop_spec(plugin_id)
    assert repr(plugin_id) in str(raised.value)
    assert all(known in str(raised.value) for known in LOOPS)
