"""Whole evolve runs on the production shell, simulated by vs-dst: the end result only.

The flow stories (`test_stories.py`) pin each decision; these runs check that the same search,
through the real core and shell, ends with what the story says.
"""

from __future__ import annotations

from tests.support.evolve_world import EvolveWorld

from vs_core.api import Finish, Settle, StartAttempt, Wait, Withdraw
from vs_dst.api import SimulatedRun, simulate


def adopted(run: SimulatedRun) -> str | None:
    """The commit the run adopted; None when no candidate was."""
    assert run.finished
    assert not run.liveness_violations
    final = run.decisions[-1]
    assert isinstance(final, Finish)
    chosen = final.result.selection
    return None if chosen is None else chosen.revision.git_commit


def starts(run: SimulatedRun) -> int:
    waits = (item.request for item in run.decisions if isinstance(item, Wait))
    return sum(isinstance(item, StartAttempt) for item in waits)


def test_a_run_adopts_its_best_individual() -> None:
    world = EvolveWorld(max_generations=1, children_per_generation=2, throughputs=(100.0, 150.0))
    run = simulate(world)

    commit = adopted(run)
    assert commit is not None
    assert starts(run) == 3  # one cold start and one generation of two children
    final = run.decisions[-1]
    assert isinstance(final, Finish)
    assert final.result.outcome == "success"
    best = max(world.headline(item) or 0.0 for item in _commits(run))
    assert world.headline(commit) == best


def _commits(run: SimulatedRun) -> list[str]:
    """The revisions the run's workspaces settled with a candidate."""
    waits = (item.request for item in run.decisions if isinstance(item, Wait))
    withdrawn = (item.disposition for item in waits if isinstance(item, Withdraw))
    return [
        item.candidate.revision_id.root
        for item in withdrawn
        if isinstance(item, Settle) and item.candidate is not None
    ]
