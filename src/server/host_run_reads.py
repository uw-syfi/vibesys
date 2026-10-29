"""A project's runs, their digests and what they spent: the reads shared by every transport.

The helper's ``runs.list`` and ``usage.window`` and the registered ``ListRunDigests`` and
``UsageWindow`` answer from these functions, so one definition says what a digest and a window
hold. Nothing here knows which transport asked.

Public surface: ``ProjectRun``, ``RunProjects``, ``RunFacts``, ``run_summaries``, ``project_runs``
and ``usage_window_reply``.
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
from server.host_envelope import HelperErrorCode
from server.host_examples import ProjectLookupError
from server.host_run_dashboard import IdeaVerdict
from server.run_digest import run_attention

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from datetime import datetime
    from pathlib import Path

    from server.host_run_summary import UsageWindowReply, UsageWindowRequest
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

    def runs(self, project: Path) -> tuple[Path, tuple[ProjectRun, ...]]:
        """The project's resolved root and its runs, oldest first."""
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
    """A project's runs, each of ``digest_runs`` with the digest its journal gives.

    The tile's data (``with_dashboard``) and the attention it judges are included only when asked.
    """
    root, runs = host.projects.runs(project)
    known = {run.run_id for run in runs}
    digests = {
        run_id: digest
        for run_id in dict.fromkeys(digest_runs)
        if run_id in known and (digest := host.projects.run_digest(root, run_id)) is not None
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


def usage_window_reply(
    request: UsageWindowRequest, host: RunFacts, project: Path
) -> UsageWindowReply:
    """Answer what the named runs spent in the window, from each run's usage records.

    A run's records are its summary's (``Projects.run_summary``, the cache beside the journal) and
    its bests are the kept points of its digest, so each run costs a cache read and what its journal
    gained since. A run the project does not have, or whose journal this machine does not hold, is
    ``missing``. ``project`` is the request's project directory, resolved on this host.
    """
    # lint-waiver: LW-271612 [PLC0415]; the helper answers hello without loading the aggregation.
    # > `server.usage_window` is a module the helper's start-up budget
    # > (`tests/e2e/test_host_helper.py`) has no room for, and only a `usage.window` request needs
    # > it; the request and reply messages stay with the other messages so `host_api` can name them.
    from server.usage_window import RunUsage, usage_window  # noqa: PLC0415

    root, known = host.projects.runs(project)
    tasks = {run.run_id: run.task for run in known}
    found: list[RunUsage] = []
    missing: list[str] = []
    for run_id in dict.fromkeys(request.run_ids):
        try:
            records = host.projects.run_summary(project, run_id).fold.records
        except ProjectLookupError as error:
            if error.code is not HelperErrorCode.UNKNOWN_RUN:
                raise
            missing.append(run_id)
            continue
        digest = host.projects.run_digest(root, run_id)
        points = (
            digest.dashboard.points if isinstance(digest, RunDigest) and digest.dashboard else ()
        )
        bests = [point.at for point in points if point.verdict is IdeaVerdict.KEPT and point.at]
        found.append(RunUsage(run_id, tasks.get(run_id), records, bests))
    return usage_window(request, found, missing)
