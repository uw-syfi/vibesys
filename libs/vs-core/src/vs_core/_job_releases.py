"""Tell a closing attempt when one of its measurement jobs has been released.

Closing an attempt records a ``job`` release dependency for every job of its scope that has
not been released yet. Nobody else announces a job's release, so without this module the
attempt keeps waiting for a release that already happened: retention and discard are withheld
while a job dependency stands, so no other event revisits the attempt either.

The evaluation area owns the job's observation, so it announces the first time a job is
recorded as released. The attempts area decides whether the observation proves the release.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.attempts import ReleaseDependency, ReleaseDependencyObserved
from .types.common import AttemptId, AttemptRef

if TYPE_CHECKING:
    from .types.common import ResourceId
    from .types.evaluation import EvaluationState, OwnedJob, RegisteredOwnedJob
    from .types.kernel import EvaluationContext, Signal


def _released_resources(state: EvaluationState) -> frozenset[ResourceId]:
    return frozenset(
        job.resource_id
        for job in (*state.jobs, *state.registered_jobs)
        if job.resource_id is not None and job.released
    )


def job_release_signals(
    before: EvaluationState, after: EvaluationState, context: EvaluationContext
) -> tuple[Signal, ...]:
    """One ``ReleaseDependencyObserved`` per job that this change recorded as released.

    Only a job whose attempt is waiting on exactly that ``job`` dependency is announced.
    """
    already = _released_resources(before)
    jobs: tuple[OwnedJob | RegisteredOwnedJob, ...] = (*after.jobs, *after.registered_jobs)
    signals: list[Signal] = []
    for job in jobs:
        if (
            job.resource_id is None
            or not job.released
            or job.observation is None
            or job.resource_id in already
            or not isinstance(job.scope.owner, AttemptId)
        ):
            continue
        dependency = ReleaseDependency(kind="job", identity=job.resource_id)
        owner = next(
            (
                row
                for row in context.attempts.attempts
                if row.attempt_id == job.scope.owner
                and row.generation == job.scope.generation
                and dependency in row.release_dependencies
            ),
            None,
        )
        if owner is not None:
            signals.append(
                ReleaseDependencyObserved(
                    attempt=AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation),
                    dependency=dependency,
                    observation=job.observation,
                )
            )
    return tuple(signals)
