"""A ``sky`` command line over in-memory clusters, for code that cancels and finds remote jobs.

It answers the two commands a job lookup and a cancel use. ``sky queue <cluster> --output json``
lists a cluster's jobs by name, id and status, and ``sky cancel -y <cluster> <id>`` ends a job
that has not ended (cancelling an ended one changes nothing), as the real CLI does. While
:meth:`unreachable` is open every command fails with a nonzero status, as a CLI that cannot
reach its control plane does.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import TYPE_CHECKING

from vs_sandbox.skypilot_runner import ProcessResult
from vs_sandbox.skypilot_states import SkyJobState

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path

_ENDED = frozenset(
    {
        SkyJobState.SUCCEEDED,
        SkyJobState.FAILED,
        SkyJobState.FAILED_SETUP,
        SkyJobState.FAILED_DRIVER,
        SkyJobState.CANCELLED,
    }
)
_CONTROL_PLANE_DOWN = 255


class FakeSkyPilotCluster:
    """The jobs of the clusters a ``sky`` client can reach."""

    def __init__(self) -> None:
        """Start with no cluster and a reachable control plane."""
        self._jobs: dict[str, dict[int, tuple[str, SkyJobState]]] = {}
        self._reachable = True
        self.calls: list[tuple[str, ...]] = []
        """Every command received, in order."""

    def start_job(self, cluster: str, name: str) -> int:
        """A job named *name* starts running on *cluster*; its id."""
        jobs = self._jobs.setdefault(cluster, {})
        job_id = len(jobs) + 1
        jobs[job_id] = (name, SkyJobState.RUNNING)
        return job_id

    def state_of(self, cluster: str, job_id: int) -> SkyJobState:
        """Where the job is now."""
        return self._jobs[cluster][job_id][1]

    def running(self, cluster: str | None = None) -> int:
        """How many jobs have not ended, on *cluster* or on every cluster."""
        return sum(
            state not in _ENDED
            for name, jobs in self._jobs.items()
            if cluster in (None, name)
            for _, state in jobs.values()
        )

    @contextmanager
    def unreachable(self) -> Iterator[None]:
        """Make every command fail while the context is open."""
        self._reachable = False
        try:
            yield
        finally:
            self._reachable = True

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        cwd: Path | None = None,
        stdout_sink: Callable[[str], None] | None = None,
        stderr_sink: Callable[[str], None] | None = None,
    ) -> ProcessResult:
        """Answer ``queue`` and ``cancel``; any other command is a test bug."""
        del timeout, cwd, stdout_sink, stderr_sink
        command = tuple(argv)
        self.calls.append(command)
        if not self._reachable:
            return ProcessResult(command, _CONTROL_PLANE_DOWN, "", "cannot reach the control plane")
        match command[1:]:
            case ("queue", cluster, "--output", "json"):
                return ProcessResult(command, 0, self._queue(cluster), "")
            case ("cancel", "-y", cluster, job_id):
                self._cancel(cluster, int(job_id))
                return ProcessResult(command, 0, "", "")
            case _:
                message = f"FakeSkyPilotCluster does not model `sky {' '.join(command[1:])}`"
                raise AssertionError(message)

    def _queue(self, cluster: str) -> str:
        jobs = self._jobs.get(cluster, {})
        return json.dumps(
            {
                cluster: [
                    {"job_id": job_id, "job_name": name, "status": state.value}
                    for job_id, (name, state) in jobs.items()
                ]
            }
        )

    def _cancel(self, cluster: str, job_id: int) -> None:
        name, state = self._jobs[cluster][job_id]
        if state not in _ENDED:
            self._jobs[cluster][job_id] = (name, SkyJobState.CANCELLED)
