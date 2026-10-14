"""The state a flow search persists grows linearly with the work it did, never faster.

A search that opens a workspace and asks an agent in it, over and over, stands for any search that
runs many children: what core keeps of one finished child is a constant, so a run of ``n`` of them
persists about ``n`` times that. Core owns this (the journal of the flow), so it is checked on a
toy flow here and not once per search.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_sim.api.testing import examples

from .flow_support import Config, Plan, ask, open_workspace, start


def persisted_after(children: int) -> list[int]:
    """The size of the persisted state at the start and after each finished child."""

    async def root(ctx: core.Flow, _config: Config) -> core.NoWinner | None:
        for number in range(children):
            async with open_workspace(ctx, f"child-{number}"):
                await ask(ctx, f"ask-{number}")
        return core.NoWinner(reason="done")

    run = start(root)
    sizes = [len(run.state.model_dump_json())]
    for _ in range(children):
        run.open_workspace(run.expect(core.StartAttempt))
        run.reply(run.expect_turn(), Plan(n=1))
        run.close_workspace(run.expect(core.Withdraw))
        sizes.append(len(run.state.model_dump_json()))
    return sizes


@settings(max_examples=examples(pr=4, nightly=100))
@given(children=st.integers(min_value=8, max_value=24))
def test_what_a_flow_persists_grows_linearly_in_the_children_it_ran(children: int) -> None:
    sizes = persisted_after(children)
    half = children // 2
    first = sizes[half] - sizes[0]
    assert first > 0
    second = sizes[children] - sizes[half]
    # Equal numbers of children add about equal sizes; a state that re-stored its past at every
    # step would make the second half several times larger than the first.
    assert second * half <= 1.5 * first * (children - half)
