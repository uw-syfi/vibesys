"""Which transcript sessions are a person's own: those no run of this host made.

A provider CLI keeps a transcript of every session it ran on the host, a run's agents included.
The runs' own usage is read from their journals, so a transcript of a run's session must not be
counted again as "outside runs". ``exclude`` decides that from facts about the host's runs, in
three layers, strongest first, and a session or turn takes the first that applies:

1. **Exact, by session.** The run's journal names the provider's id of the conversation each
   turn ran in (``provider_session_ids``). A transcript session with one of those ids is the
   run's. Runs recorded since the journal began to name them.
2. **Exact, by place.** A candidate worktree is at ``.vibesys/state/local/runs/<run id>/worktrees/``
   and only a run makes that path, so a session whose working directory is under it is a run's,
   old or new, even when the run is gone.
3. **Approximate, by time and place.** An older run that did not name every session (its
   ``session_ids_complete`` is false) may have worked at its project's root, which is also where
   a person might run the CLI. A transcript turn of the same provider, at that root, within the
   run's first to last turn, is taken as the run's. It is a guess both ways, so it is counted
   apart and shown as approximate. Nothing is ever excluded by it for a run that named its
   sessions, and it expires with the runs it covers.

The function is pure: the working-directory test is passed in (``vs_project`` owns the layout),
and nothing is read from disk. Rows are the transcript cache's, already added by quarter hour.

Light: ``vs_proto`` only.

Public surface: ``RunFacts``, ``ProviderRow``, ``Outside`` and ``exclude``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from server.usage_rollup import QUARTER_SECONDS

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from vs_proto.api import usage_state


@dataclass(frozen=True)
class RunFacts:
    """What the rollup of one run says that decides whether a transcript session is its own."""

    run_id: str
    project_root: str | None
    """The run's project directory; ``None`` when the host has no address for the run."""
    providers: frozenset[str]
    session_ids: frozenset[str]
    session_ids_complete: bool
    first_turn_at: datetime | None
    last_turn_at: datetime | None


@dataclass(frozen=True)
class ProviderRow:
    """One row of a provider's transcript cache."""

    provider: str
    row: usage_state.TranscriptRow


@dataclass(frozen=True)
class Outside:
    """The rows no run claims, and what was left out of the rest."""

    kept: tuple[ProviderRow, ...]
    excluded_sessions: dict[str, frozenset[str]] = field(default_factory=dict)
    """By provider, the sessions left out exactly (layers 1 and 2) among the rows counted."""
    excluded_turns: dict[str, int] = field(default_factory=dict)
    """By provider, the turns left out on a guess (layer 3) among the rows counted."""


def exclude(
    rows: Sequence[ProviderRow],
    runs: Sequence[RunFacts],
    workspace_run: Callable[[str], str | None],
    *,
    counted: tuple[int, int] | None = None,
) -> Outside:
    """The rows of ``rows`` that no run of ``runs`` made.

    ``workspace_run`` says which run a working directory belongs to by the path alone. The counts
    of what was excluded cover the rows whose quarter hour starts in ``counted`` (epoch seconds,
    start inclusive, end exclusive), all of them by default; the kept rows are every row left.
    """
    named = frozenset(identifier for run in runs for identifier in run.session_ids)
    guessing = [run for run in runs if not run.session_ids_complete and run.project_root]
    kept: list[ProviderRow] = []
    sessions: dict[str, set[str]] = {}
    turns: dict[str, int] = {}
    for found in rows:
        row = found.row
        within = counted is None or counted[0] <= row.quarter * QUARTER_SECONDS < counted[1]
        if row.session_id in named or (row.cwd and workspace_run(row.cwd) is not None):
            if within:
                sessions.setdefault(found.provider, set()).add(row.session_id)
            continue
        if any(_guessed(found, run) for run in guessing):
            if within:
                turns[found.provider] = turns.get(found.provider, 0) + row.turns
            continue
        kept.append(found)
    return Outside(
        tuple(kept),
        {provider: frozenset(found) for provider, found in sessions.items()},
        turns,
    )


def _guessed(found: ProviderRow, run: RunFacts) -> bool:
    """Whether ``found`` is taken as ``run``'s on the evidence of time and place alone."""
    row = found.row
    if (
        found.provider not in run.providers
        or run.first_turn_at is None
        or run.last_turn_at is None
        or row.cwd != run.project_root
    ):
        return False
    begins = row.quarter * QUARTER_SECONDS
    return begins + QUARTER_SECONDS > run.first_turn_at.timestamp() and begins <= (
        run.last_turn_at.timestamp()
    )
