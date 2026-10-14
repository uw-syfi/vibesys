"""How the search stories restart: at sampled steps on a pull request, at every step nightly.

Restarting round-trips the whole flow state through JSON and replays the search, so its cost
grows with every restart; vs-core proves restart equivalence for any flow once. A search story
only has to show that *its* decisions survive a restart, so a pull request restarts at a few
points (``PINNED`` plus one drawn from the session's randomness) and the nightly job at each
step. ``across_restarts`` runs a story live and restarting; ``start_flow`` is the one place the
search hosts start a ``FlowRun``.
"""

from __future__ import annotations

import functools
import secrets
from typing import TYPE_CHECKING, TypedDict, Unpack

import pytest

from vs_core.api.testing import FlowRun, RestartPolicy
from vs_sim.api.testing import Depth, depth

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import RunFacts
    from vs_core.api.testing import FakeView
    from vs_core.testing.flows import SearchDef

PINNED = (0, 1, 2, 5)
"""Steps every pull request restarts after: the first few, where a restart is cheap and a stop or a late task meets a fresh driver."""
DRAW_UP_TO = 60
"""A drawn restart step is in ``[0, DRAW_UP_TO)``; a story with fewer steps never reaches it."""


def sampled_restarts(drawn: int) -> RestartPolicy:
    """The restarts of a session: every step nightly; ``PINNED`` and ``drawn`` otherwise."""
    if depth() is Depth.NIGHTLY:
        return RestartPolicy(every=True)
    return RestartPolicy(steps=frozenset({*PINNED, drawn}), after_stop=True)


class FlowOptions(TypedDict, total=False):
    """What a story may set when it starts a run, besides how it restarts."""

    view: FakeView | None
    adopting: bool
    facts: RunFacts


def start_flow[C](
    search: SearchDef[C],
    config: C,
    *,
    restarting: bool | RestartPolicy,
    **options: Unpack[FlowOptions],
) -> FlowRun[C]:
    """Start ``search`` under a ``FlowRun`` that restarts as ``restarting`` says."""
    return FlowRun.start(search, config, restarting=restarting, **options)


def across_restarts[F: Callable[..., None]](story: F) -> F:
    """Run a story restarting as the session's depth says (nightly: live as well).

    A restarting run shows everything a live run does (restart equivalence is vs-core's), so a
    pull request runs the story once, restarted at a few steps; the nightly job also runs it
    live, to tell a broken search from a broken restart, and restarts after every step.

    The story takes ``restarting`` (False, or a ``RestartPolicy``) and passes it on to its host. A
    failure names the steps the restarting run restarted after, so it can be replayed.
    """

    @functools.wraps(story)
    def run(*args: object, **kwargs: object) -> None:
        if not kwargs["restarting"]:
            story(*args, **kwargs)
            return
        kwargs["restarting"] = plan = sampled_restarts(_draw())
        try:
            story(*args, **kwargs)
        except BaseException as error:
            error.add_note(
                f"restarted after steps {'all' if plan.every else sorted(plan.steps)} and after a stop"
            )
            raise

    if depth() is Depth.NIGHTLY:
        modes, ids = [False, True], ["live", "restarting"]
    else:
        modes, ids = [True], ["restarting"]
    return pytest.mark.parametrize("restarting", modes, ids=ids)(run)  # ty: ignore[invalid-return-type]


def _draw() -> int:
    """A step to restart after, different on every run (a failure reports it)."""
    return secrets.randbelow(DRAW_UP_TO)
