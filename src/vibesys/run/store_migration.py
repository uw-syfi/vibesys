"""One-time migration of a run saved before the cutover (``store.json``) into its ``run.log``.

TEMPORARY: this module and its tests are deleted seven days after the cutover ships. That date
is not checked by a test, because a check on the date would depend on real time; the pull request
that ships the cutover records it, and so does docs/contributing/run-recovery.md. The log's reader
of ``CoreSnapshot`` is not temporary: a migrated run keeps its snapshot for good.

A run that began before the cutover keeps its core state, its search and the owner events it had
not applied in ``store.json``. The migration appends them to the run's log as one ``CoreSnapshot``
record, so a crash leaves the log with all of it or none, and a resume migrates again only if the
log still holds none. The store is left as it was. Two things of the saved run are not carried over:
the fence and the event cursor (the run lock replaces the one and the log's sequence the other),
and the publications to the display journal it had not yet delivered.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

from vs_core.api.recovery import SavedRun, starts_run
from vs_proto.api import RUN_LOG_MAX_RECORD_BYTES
from vs_runlog.api import Appended
from vs_runtime.api.core import resolve_core_resume
from vs_runtime.api.step_runner import snapshot_message

if TYPE_CHECKING:
    from collections.abc import Callable

    from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

    from vs_core.api import OperationRegistry, SearchDriver, StrategyState
    from vs_project.api import Project
    from vs_runlog.api import ConfirmedLog
    from vs_runtime.api.core import RuntimeRecord


class Migration(StrEnum):
    """What a resume found, and did, about a run saved before the cutover. Closed."""

    MIGRATED = "migrated"
    """The log held no start of the run and the store did: the snapshot is appended."""
    STARTED = "started"
    """The log already holds the start of the run (a snapshot, or a new run's seed): nothing to do."""
    NOTHING_SAVED = "nothing_saved"
    """Neither the log nor the store holds the run: it is a new run, and the caller seeds it."""


class SnapshotTooLargeError(ValueError):
    """The saved run does not fit one record, so it cannot be appended whole."""

    def __init__(self, run_id: str, size: int) -> None:
        """Name the run and the sizes."""
        super().__init__(
            f"the saved state of run {run_id} is {size} bytes compressed; "
            f"a run.log record holds {RUN_LOG_MAX_RECORD_BYTES}"
        )


class SnapshotNotAppendedError(OSError):
    """The append of the snapshot could not be confirmed: it may or may not be in the log.

    The next resume reads the log and learns which.
    """

    def __init__(self, run_id: str) -> None:
        """Name the run."""
        super().__init__(f"the snapshot of run {run_id} could not be confirmed in its run.log")


def saved_run(record: RuntimeRecord[Any]) -> SavedRun:
    """What the log's snapshot holds of ``record``, a run saved before the cutover."""
    return SavedRun(
        state=record.envelope.core,
        envelope_version=record.envelope.schema_version,
        strategy=record.envelope.strategy.model_dump_json().encode(),
        inputs=record.pending_inputs,
        wakes=record.wakes,
        held=record.pending_decisions,
    )


def migrate_saved_run[S: StrategyState](  # noqa: PLR0913  # lint-waiver: LW-310227 [PLR0913]; the project and run that hold the store, the log and its stamp that take the snapshot, and the search and registry that read it are one migration's six inputs, each used once.
    # > A settings object would be built at the one call site and read in the same place.
    project: Project,
    run_id: str,
    log: ConfirmedLog,
    *,
    search: SearchDriver[S, Any],
    registry: OperationRegistry,
    epoch: int,
    stamp: Callable[[], Timestamp],
) -> Migration:
    """Append the snapshot of ``run_id``'s ``store.json`` to ``log``, if the log holds no start.

    ``epoch`` is the epoch the resuming process writes, whose header the log already holds.
    Raises ``CoreResumeError`` when the store cannot be resumed by ``search``,
    ``SnapshotTooLargeError`` when the run does not fit a record, and ``SnapshotNotAppendedError``
    when the append could not be confirmed.
    """
    if any(starts_run(record) for record in log.records()):
        return Migration.STARTED
    if project.stored_record(run_id) is None:
        return Migration.NOTHING_SAVED
    resolved = resolve_core_resume(project, search, run_id=run_id, registry=registry)
    saved = saved_run(resolved.record)
    message = snapshot_message(saved)
    with log.transaction():
        snapshot = log.next_record(message, epoch=epoch, at=stamp(), clock=saved.state.run.now_at)
        size = len(snapshot.to_binary())
        if size > RUN_LOG_MAX_RECORD_BYTES:
            raise SnapshotTooLargeError(run_id, size)
        if not isinstance(log.extend(snapshot), Appended):
            raise SnapshotNotAppendedError(run_id)
    return Migration.MIGRATED
