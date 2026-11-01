"""A simulated run is live when it ends as it should."""

from __future__ import annotations

from vs_dst.api import simulate

from ._tiny import TinyWorld


def test_a_run_that_ends_the_way_it_should_is_live() -> None:
    run = simulate(TinyWorld(2))

    assert run.finished
    assert not run.liveness_violations
