"""The whole-loop harness resolves a strategy by plugin id and refuses an unknown one by name."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.loop_spec import LOOPS, UnknownLoopError, loop_spec

from vibesys.dynamic_core import dynamic_projector
from vibesys.issue_queue_core import issue_queue_projector

PROJECTORS = {"dynamic": dynamic_projector, "plain": issue_queue_projector}
"""How the product reads each strategy's run back: the operations it decodes the log with."""


def test_every_registered_strategy_resolves_to_its_own_spec() -> None:
    assert LOOPS
    assert all(loop_spec(plugin_id) is spec for plugin_id, spec in LOOPS.items())


@given(plugin_id=st.text(max_size=12).filter(lambda text: text not in LOOPS))
def test_an_unknown_plugin_id_is_refused_naming_it_and_the_known_ids(plugin_id: str) -> None:
    with pytest.raises(UnknownLoopError) as raised:
        loop_spec(plugin_id)
    assert repr(plugin_id) in str(raised.value)
    assert all(known in str(raised.value) for known in LOOPS)


def test_every_registered_strategy_is_read_back_by_the_product() -> None:
    assert sorted(PROJECTORS) == sorted(LOOPS)


@pytest.mark.parametrize("plugin_id", sorted(PROJECTORS))
def test_the_harness_decodes_a_run_log_with_the_operations_the_product_reads_it_with(
    plugin_id: str,
) -> None:
    """A record of an operation the harness's registry lacks fails the fold of a finished run.

    The dynamic search writes its handoff operation beside the kit's; a registry of the kit alone
    refused the first proposal that named it, after the run had completed.
    """
    ours = loop_spec(plugin_id).registry().descriptors
    assert ours == PROJECTORS[plugin_id]().operations.descriptors
