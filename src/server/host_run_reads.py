"""A project's runs and their digests: the reads shared by every transport.

The helper's ``runs.list`` and the registered ``ListRunDigests`` answer from these functions, so
one definition says what a digest holds. Nothing here knows which transport asked.

Public surface: ``ProjectRun``, ``RunProjects``, ``RunFacts``, ``run_summaries``, ``project_runs``
and ``named_run_digests``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from server.host_api import (
    InstanceList,
    LiveInstanceRecord,
    RunDigest,
    RunDigestUnreadable,
    RunList,
    RunSummary,
)
from server.run_digest import run_attention

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping
    from datetime import datetime
    from pathlib import Path

    from server.registry import LiveRegistry
    from server.run_summary import Summarized
    from vs_sim.api import Clock


@dataclass(frozen=True)
class ProjectRun:
    """One run a project's durable state records."""

    run_id: str
    task: str | None
    display_name: str
    created_at: datetime


class RunProjects(Protocol):
    """The part of a project catalog the reads of a project's runs need."""

    def stream_runs(self, project: Path) -> tuple[Path, Iterator[ProjectRun]]:
        """The project's resolved root and one lazy scan of its runs, in discovery order."""
        ...

    def named_runs(
        self, project: Path, run_ids: Iterable[str]
    ) -> tuple[Path, tuple[ProjectRun, ...]]:
        """The project's resolved root and those of ``run_ids`` it holds, oldest first."""
        ...

    def run_digest(self, project_root: Path, run_id: str) -> RunDigest | RunDigestUnreadable | None:
        """What run ``run_id``'s journal says at a glance; ``None`` when this machine holds none."""
        ...

    def run_summary(self, project: Path, run_id: str) -> Summarized:
        """The fold of run ``run_id``'s whole journal, as it is now."""
        ...


class RunFacts(Protocol):
    """What the reads of a project's runs need of a host: its projects, registry and clock."""

    @property
    def projects(self) -> RunProjects:
        """The project catalog and the runs' journals."""
        ...

    @property
    def registry(self) -> LiveRegistry:
        """The servers this host runs or ran."""
        ...

    @property
    def clock(self) -> Clock:
        """Wall-clock epoch seconds."""
        ...


def project_runs(
    host: RunFacts, project: Path, digest_runs: Iterable[str], *, with_dashboard: bool
) -> RunList:
    """A project's runs, oldest first, each of ``digest_runs`` with the digest its journal gives.

    The tile's data (``with_dashboard``) and the attention it judges are included only when asked.
    """
    root, scan = host.projects.stream_runs(project)
    runs = tuple(sorted(scan, key=lambda run: (run.created_at, run.run_id)))
    known = {run.run_id for run in runs}
    return _digested(
        host,
        root,
        runs,
        (run_id for run_id in dict.fromkeys(digest_runs) if run_id in known),
        with_dashboard=with_dashboard,
    )


def named_run_digests(
    host: RunFacts, project: Path, run_ids: Iterable[str], *, with_dashboard: bool
) -> RunList:
    """Those of ``run_ids`` the project holds, each with its digest, at a cost set by ``run_ids``.

    Unlike ``project_runs``, this leaves every other run out, so a project's size costs it
    nothing: the tiles ask for this every few seconds while a run is live.
    """
    root, runs = host.projects.named_runs(project, run_ids)
    return _digested(host, root, runs, (run.run_id for run in runs), with_dashboard=with_dashboard)


def _digested(
    host: RunFacts,
    root: Path,
    runs: tuple[ProjectRun, ...],
    digest_runs: Iterable[str],
    *,
    with_dashboard: bool,
) -> RunList:
    digests = {
        run_id: digest
        for run_id in digest_runs
        if (digest := host.projects.run_digest(root, run_id)) is not None
    }
    if not with_dashboard:
        digests = {
            run_id: digest.model_copy(update={"dashboard": None})
            if isinstance(digest, RunDigest)
            else digest
            for run_id, digest in digests.items()
        }
    now = host.clock.now()
    return run_summaries(
        root, runs, host.registry.list(), digests, now=now if with_dashboard else None
    )


def run_summaries(
    root: Path,
    runs: Iterable[ProjectRun],
    listing: InstanceList,
    digests: Mapping[str, RunDigest | RunDigestUnreadable] | None = None,
    *,
    now: float | None = None,
) -> RunList:
    """Join a project's runs with this host's registry: each run's server and its phase.

    A live server outranks a tombstone; among tombstones the latest ended wins. A run's
    ``digest`` is the one ``digests`` holds for it, if any. With ``now`` (epoch seconds), each
    run whose digest carries a dashboard also gets its ``attention`` as of ``now``.
    """
    project_root = str(root)
    live = {
        record.run_id: record for record in listing.instances if record.project_root == project_root
    }
    ended: dict[str, LiveInstanceRecord] = {}
    for record in sorted(listing.ended, key=lambda record: record.ended_at or 0.0):
        if record.project_root == project_root:
            ended[record.run_id] = record
    summaries = []
    for run in runs:
        server = live.get(run.run_id) or ended.get(run.run_id)
        digest = None if digests is None else digests.get(run.run_id)
        phase = None if server is None else server.phase
        judged = isinstance(digest, RunDigest) and digest.dashboard is not None and now is not None
        summaries.append(
            RunSummary(
                run_id=run.run_id,
                task=run.task,
                display_name=run.display_name,
                created_at=run.created_at,
                phase=phase,
                instance_id=None if run.run_id not in live else live[run.run_id].id,
                digest=digest,
                attention=run_attention(digest, phase=phase, now=now) if judged else None,
            )
        )
    return RunList(project_root=project_root, runs=tuple(summaries))
