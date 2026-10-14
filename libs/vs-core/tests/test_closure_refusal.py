"""A closing attempt never waits on a request the workspace owner answered "no".

A request answered conclusively without success holds nothing, so no release can follow it. Core
settles such an attempt with a typed failure instead of waiting for ever: a refused retention
means the candidate was not kept (the settlement is failed, the workspace is discarded), and a
refused discard or scope close leaves nothing more to ask. This is the same class as a rejected
workspace request that stalled a closing attempt (see ``test_rejected_request_release``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.testing import Failed
from vs_sim.api.testing import examples

from .test_peer_turn_failure import Fault, PeerHost, assert_released, run_peers

if TYPE_CHECKING:
    from vs_core.api.testing import Answer, Trace

# Every closure request that asks the workspace to keep or drop the candidate. A refused scope
# fence or ``CloseSession`` is not among them: neither proves its writers or session gone, so
# releasing on the refusal would be unsafe and core keeps waiting for a proof of release.
REFUSED_KINDS = (
    core.RetainRevision,
    core.SnapshotAndRetain,
    core.DiscardWorkspace,
)


def after_next_tick(trace: Trace) -> Trace:
    """The trace once the clock ticks again: a run whose last cleanup ended after its slot was
    released is drained by the next tick, which the shell's clock always delivers."""
    if not trace.finished:
        ticked = core.step(trace.core, core.ClockAdvanced(now_at=trace.core.run.now_at + 1.0))
        trace.core = ticked.state
        trace.finished = ticked.state.run.status == core.RunStatus.TERMINAL
    return trace


@dataclass
class RefusingHost(PeerHost):
    """The peers host whose workspace owner refuses every request of the kinds in ``refuse``."""

    refuse: tuple[type, ...] = ()

    def __call__(self, request: core.Request, state: core.CoreState) -> Answer:
        """What the executor of ``request`` reports."""
        if isinstance(request, self.refuse):
            return Failed()
        return super().__call__(request, state)


def test_a_retention_the_owner_refuses_at_closure_fails_the_settlement_and_releases_the_attempt() -> (
    None
):
    trace = run_peers(("ok",), host=RefusingHost(script=("ok",), refuse=(core.RetainRevision,)))

    assert_released(after_next_tick(trace))
    [settlement] = trace.core.settlement.settlements
    assert settlement.outcome == "failed"
    assert settlement.retention == "discard"
    assert not settlement.eligible


@settings(max_examples=examples(pr=3, nightly=40), deadline=None, derandomize=True, database=None)
@given(
    refuse=st.sets(st.sampled_from(REFUSED_KINDS), min_size=1).map(tuple),
    script=st.lists(st.sampled_from(("ok", "crash")), min_size=1, max_size=3).map(tuple),
)
def test_an_attempt_ends_terminal_whichever_closure_requests_the_owner_refuses(
    refuse: tuple[type, ...], script: tuple[Fault, ...]
) -> None:
    trace = run_peers(script, host=RefusingHost(script=script, refuse=refuse))

    assert_released(after_next_tick(trace))
