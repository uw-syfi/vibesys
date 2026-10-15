"""A detached run server's first step: publish its registry record, then load the engine.

``vibesys --detach`` allocates the instance id and the run id, then starts
``python -m entrypoints.detached_run`` with both. This module takes the
instance's lifetime lock and publishes a record that names the run before it
imports anything heavy, so ``vibesys instances list`` shows the run with its id
within the interpreter's start-up time, not after the engine's (about 1.7 s)
or the run's setup. It then serves the run through ``entrypoints.server``,
which reports its milestones and its run's lifecycle facts to the
``RecordPublisher`` held here. However the server exits, the publisher leaves
its last record, with the phase the run ended in, as the instance's tombstone.

This module is light: ``tests/architecture/test_light_entrypoints.py`` holds
its module-level imports to the same allowlist as the host commands.
"""

from __future__ import annotations

import argparse
import os
import sys
from functools import partial
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING

from entrypoints.launcher import source_checkout_root
from server.host_api import (
    FailureReason,
    InstanceInspectionMetadata,
    InstanceStatus,
    LiveInstanceRecord,
    RunPhase,
    RunStatus,
    StartupFact,
    StartupStage,
)
from server.registry import (
    FileInstanceMetadataStore,
    FileInstanceStore,
    InstanceHold,
    InstanceMetadataStore,
    LiveRegistry,
    host_facts,
    instance_log_path,
    instance_root,
    parse_instance_id,
)
from vs_project.api import ProjectStateError, validate_run_id
from vs_sim.api import OsThreads, SystemClock

if TYPE_CHECKING:
    from vibesys.api import Runs
    from vs_sim.api import Clock, Threads

SERVER_EXITED = FailureReason(
    code="server_exited",
    stage="server",
    message="The server exited before its run ended; see its log.",
)
"""The reason a tombstone records when the run never reached an ended phase."""

PROGRESS_WRITE_INTERVAL_SECONDS = 1.0
"""Least time between two record writes that change only a stage's progress.

A stage change, and every other phase change, is written at once; progress
within a stage (an image build's steps) is written at most once per interval,
so a fast build does not rewrite the record per step.
"""


class RecordPublisher:
    """Keep a held instance's record current, and leave it as the tombstone.

    The record's phase is folded here (``RunPhase.advance``) from the facts the
    server reports; ``end`` writes the final record. A change of startup
    progress alone is written at most once per
    ``PROGRESS_WRITE_INTERVAL_SECONDS``: one arriving sooner is held, and a
    worker on ``threads`` writes the latest held phase when the interval ends,
    unless another write carried it first.
    """

    def __init__(
        self,
        hold: InstanceHold,
        record: LiveInstanceRecord,
        clock: Clock,
        *,
        metadata: InstanceMetadataStore,
        threads: Threads,
    ) -> None:
        """Publish ``record`` through ``hold`` now; ``clock`` stamps the tombstone.

        ``threads`` paces progress writes.
        """
        self._hold = hold
        self._metadata = metadata
        self._record = record
        self._clock = clock
        self._threads = threads
        self._lock = self._threads.lock()
        self._held: RunPhase | None = None
        self._flush_scheduled = False
        self._written_at = self._threads.now()
        hold.publish(record)

    def resolved(self, *, project_root: Path, task: str) -> None:
        """Retain the immutable identity after the invocation selected its project and task."""
        resolved_root = str(project_root.resolve())
        with self._lock:
            self._publish(project_root=resolved_root)
            record = self._record
        self._metadata.publish(
            InstanceInspectionMetadata(
                id=record.id,
                project_root=resolved_root,
                run_id=record.run_id,
                task=task,
                pid=record.pid,
                started_at=record.started_at,
                hostname=record.hostname,
                protocol_version=record.protocol_version,
                vibesys_version=record.vibesys_version,
                vibesys_root=record.vibesys_root,
                control_socket_path=record.socket_path,
                server_log_path=record.log_path,
            )
        )

    def listening(self) -> None:
        """The control socket accepts connections."""
        with self._lock:
            self._publish(status=InstanceStatus.SERVING)

    def observe(
        self,
        *,
        status: RunStatus | None = None,
        failure: FailureReason | None = None,
        startup: StartupFact | None = None,
    ) -> None:
        """Fold one lifecycle fact into the record's phase, publishing a change."""
        with self._lock:
            current = self._held or self._record.phase
            phase = current.advance(status=status, failure=failure, startup=startup)
            if phase == current:
                return
            written = self._record.phase
            progress_only = phase.status is written.status and phase.stage is written.stage
            wait = self._written_at + PROGRESS_WRITE_INTERVAL_SECONDS - self._threads.now()
            if progress_only and wait > 0:
                self._held = phase
                if not self._flush_scheduled:
                    self._flush_scheduled = True
                    self._threads.spawn(partial(self._flush, wait), name="record-progress")
                return
            self._publish(phase=phase)

    def end(self) -> None:
        """Write the tombstone: the phase the run ended in, or ``SERVER_EXITED``."""
        self.observe(failure=SERVER_EXITED)
        with self._lock:
            self._publish(ended_at=self._clock.now())

    def _flush(self, wait: float) -> None:
        """Write the held phase once the interval since the latest write has passed.

        Another write may land while this worker sleeps (and restart the
        interval), so the remaining wait is recomputed from the latest write
        each time the worker wakes, rather than trusting the wait it was
        spawned with.
        """
        while True:
            self._threads.sleep(wait)
            with self._lock:
                wait = self._written_at + PROGRESS_WRITE_INTERVAL_SECONDS - self._threads.now()
                if self._held is None or wait <= 0:
                    self._flush_scheduled = False
                    if self._held is not None:
                        self._publish(phase=self._held)
                    return

    def _publish(self, **update: object) -> None:
        """Write the record with ``update``; the caller holds ``self._lock``."""
        if "phase" not in update and self._held is not None:
            update["phase"] = self._held
        self._held = None
        self._record = LiveInstanceRecord.model_validate({**self._record.model_dump(), **update})
        self._written_at = self._threads.now()
        self._hold.publish(self._record)


def _run_id(value: str) -> str:
    try:
        return validate_run_id(value)
    except ProjectStateError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _identity(arguments: list[str]) -> argparse.Namespace:
    """Read the ids the launcher allocated; the rest of ``arguments`` is the run's."""
    parser = argparse.ArgumentParser(prog="entrypoints.detached_run", allow_abbrev=False)
    parser.add_argument("--instance-id", type=parse_instance_id, required=True)
    parser.add_argument("--run-id", type=_run_id, required=True)
    parser.add_argument("--control-socket", type=Path, required=True)
    identity, _ = parser.parse_known_args(arguments)
    return identity


def _starting_record(identity: argparse.Namespace, root: Path, clock: Clock) -> LiveInstanceRecord:
    hostname, version = host_facts()
    checkout = source_checkout_root()
    return LiveInstanceRecord(
        id=identity.instance_id,
        status=InstanceStatus.STARTING,
        phase=RunPhase(status=RunStatus.STARTING, stage=StartupStage.RESOLVING),
        socket_path=str(identity.control_socket),
        log_path=str(instance_log_path(root, identity.instance_id)),
        project_root=str(Path.cwd().resolve()),
        run_id=identity.run_id,
        pid=os.getpid(),
        started_at=clock.now(),
        hostname=hostname,
        vibesys_version=version,
        vibesys_root=None if checkout is None else str(checkout),
    )


def main(argv: list[str] | None = None, *, runs: Runs | None = None) -> None:
    """Hold this server's registry id for its lifetime and serve its run."""
    arguments = sys.argv[1:] if argv is None else argv
    identity = _identity(arguments)
    root = instance_root(os.environ, os.getuid())
    clock = SystemClock()
    with LiveRegistry(FileInstanceStore(root), clock).register(identity.instance_id) as hold:
        publisher = RecordPublisher(
            hold,
            _starting_record(identity, root, clock),
            clock,
            metadata=FileInstanceMetadataStore(root),
            threads=OsThreads(),
        )
        try:
            import_module("entrypoints.server").main(arguments, observer=publisher, runs=runs)
        finally:
            publisher.end()


if __name__ == "__main__":
    main()
