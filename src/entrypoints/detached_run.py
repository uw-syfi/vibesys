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

Before anything else, it installs the run's narration handler on the root logger and
the uncaught-exception hooks (``sys.excepthook``, ``threading.excepthook``). Records and
tracebacks wait in the handler's bounded buffer until run assembly opens the run's log
store (``vibesys.run.logs.open_run_logs``) and attaches the narration stream, which the
desktop app shows. What dies before that (a bad argument, an import error, a project
that does not resolve) is a typed launch failure on the record, or reaches only
``server.log`` for the person reading the terminal output; logging's last resort keeps
writing warnings to stderr as it did, so ``server.log`` shows what it always showed.

This module is light: ``tests/architecture/test_light_entrypoints.py`` holds
its module-level imports to the same allowlist as the host commands.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from contextlib import contextmanager
from functools import partial
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING

from entrypoints.launcher import source_checkout_root
from server.host_api import (
    EndRecord,
    FailureReason,
    InstanceInspectionMetadata,
    InstanceStatus,
    LiveInstanceRecord,
    PauseCause,
    RunEndingFact,
    RunEndOutcome,
    RunPhase,
    RunStatus,
    StartupFact,
    StartupStage,
    one_line,
)
from server.registry import (
    INSTANCES_DIRECTORY,
    READERS_DIRECTORY,
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
from vs_logs.api import NarrationHandler, uncaught_hook, uncaught_thread_hook
from vs_project.api import ProjectStateError, validate_run_id
from vs_sim.api import OsThreads, SystemClock

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vibesys.api import RunReaders, Runs
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


_PAUSE_STATUSES = frozenset({RunStatus.PAUSING, RunStatus.PAUSED})

_PLAIN_REASONS: dict[RunEndOutcome, str] = {
    RunEndOutcome.ADOPTED: "The chosen candidate was adopted.",
    RunEndOutcome.KEPT_INPUT: "Nothing beat the input; the input was kept.",
    RunEndOutcome.NO_WINNER: "The search ended with no winner.",
    RunEndOutcome.ADOPTION_FAILED: "The chosen winner could not be adopted.",
    RunEndOutcome.STOPPED: "Stopped by request.",
    RunEndOutcome.DEADLINE: "The run deadline passed.",
    RunEndOutcome.FINISH_REFUSED: "Core refused the run's result.",
    RunEndOutcome.FAILED: "The run failed.",
    RunEndOutcome.FORCE_STOPPED: "Force stopped before a safe point.",
}
"""The reason an end record gives when nothing more specific was reported."""


_FINAL_STOPS = frozenset({RunEndOutcome.FORCE_STOPPED})
"""Outcomes of a stop a resume cannot lift: a killed server's recorded stop request stands.

A stop the run itself committed (``STOPPED``) is lifted by the operator's resume, as long as the
run's ending has not committed (core's own ``resumable``, `run_ending`)."""


def pause_cause_after(
    pending: PauseCause | None, before: RunStatus, after: RunStatus
) -> PauseCause | None:
    """The cause a pause waits under once the run moved from ``before`` to ``after``.

    A cause is reported before the pause it explains (``quota_paused`` comes ahead of the
    status change), so it is kept until the run leaves its pause.
    """
    if before in _PAUSE_STATUSES and after not in _PAUSE_STATUSES:
        return None
    return pending


def paused_by(phase: RunPhase, pending: PauseCause | None) -> PauseCause | None:
    """Who paused a run in ``phase``: the reported cause, else the operator."""
    if phase.status is not RunStatus.PAUSED:
        return None
    return pending or PauseCause.USER


def end_record(
    phase: RunPhase, ending: RunEndingFact | None, *, terminated: bool
) -> EndRecord | None:
    """The tombstone's ``end`` for a run that ended in ``phase``; ``None`` when nothing names it.

    A terminated server's run was force stopped; otherwise core's end reason names the
    outcome, else the ended phase does. A run with no durable record that ``ending``
    vouched for is not resumable, nor is a force stopped one (its recorded stop stands). A run
    stopped by the operator is, until its ending commits: a resume lifts the stop. A completed run that reported no ending has no end.
    """
    outcome = _outcome(phase, ending, terminated=terminated)
    if outcome is None:
        return None
    reported = ending.summary if ending is not None and ending.outcome is outcome else ""
    failure = "" if phase.reason is None else phase.reason.message
    reason = one_line(reported) or one_line(failure) or _PLAIN_REASONS[outcome]
    resumable = ending is not None and ending.resumable and outcome not in _FINAL_STOPS
    return EndRecord(
        outcome=outcome,
        reason=reason,
        resumable=resumable,
        resume_from_round=ending.rounds_finished + 1 if ending is not None and resumable else None,
    )


def _outcome(
    phase: RunPhase, ending: RunEndingFact | None, *, terminated: bool
) -> RunEndOutcome | None:
    if terminated:
        return RunEndOutcome.FORCE_STOPPED
    if ending is not None and ending.outcome is not None:
        return ending.outcome
    match phase.status:
        case RunStatus.STOPPED:
            return RunEndOutcome.STOPPED
        case RunStatus.FAILED:
            return RunEndOutcome.FAILED
        case _:
            return None


class RecordPublisher:
    """Keep a held instance's record current, and leave it as the tombstone.

    The record's phase is folded here (``RunPhase.advance``) from the facts the
    server reports, and ``paused`` with it (``paused_by``); ``end`` writes the final
    record with its ``end`` (``end_record``). A change of startup
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
        self._pause_cause: PauseCause | None = None
        self._ending: RunEndingFact | None = None
        self._terminated = False
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
            self._pause_cause = pause_cause_after(self._pause_cause, current.status, phase.status)
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

    def pausing_for(self, cause: PauseCause) -> None:
        """The run pauses for ``cause``; a run already paused now reports it."""
        with self._lock:
            self._pause_cause = cause
            phase = self._held or self._record.phase
            if paused_by(phase, cause) != self._record.paused:
                self._publish(phase=phase)

    def ending(self, fact: RunEndingFact) -> None:
        """Keep how the run's durable record stood as it ended, for the tombstone."""
        with self._lock:
            self._ending = fact

    def terminated(self) -> None:
        """The server was terminated; its tombstone reports a force stop."""
        with self._lock:
            self._terminated = True

    def end(self) -> None:
        """Write the tombstone: the phase the run ended in, or ``SERVER_EXITED``, and its end."""
        self.observe(failure=SERVER_EXITED)
        with self._lock:
            phase = self._held or self._record.phase
            end = end_record(phase, self._ending, terminated=self._terminated)
            self._publish(phase=phase, ended_at=self._clock.now(), end=end)

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

    def _publish(self, phase: RunPhase | None = None, **update: object) -> None:
        """Write the record at ``phase`` (else the held or written one) with ``update``.

        ``paused`` follows the phase written. The caller holds ``self._lock``.
        """
        written = phase or self._held or self._record.phase
        update |= {"phase": written, "paused": paused_by(written, self._pause_cause)}
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
    parser.add_argument("--reopen", type=Path, default=None)
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


@contextmanager
def narration_installed() -> Iterator[NarrationHandler]:
    """Send this process's log records and uncaught exceptions to a narration handler.

    The handler buffers until run assembly attaches the run's narration stream.
    Installing a root handler retires ``logging.lastResort``, so it is installed too:
    warnings still reach stderr (``server.log``) as before. Leaving the block closes the
    handler and restores the logging configuration and hooks it found.
    """
    handler = NarrationHandler(warn=sys.stderr.write)
    root = logging.getLogger()
    installed = [handler] if logging.lastResort is None else [handler, logging.lastResort]
    level = root.level
    previous_hook = sys.excepthook
    previous_thread_hook = threading.excepthook
    for each in installed:
        root.addHandler(each)
    root.setLevel(logging.INFO)
    sys.excepthook = uncaught_hook(handler, previous_hook)
    threading.excepthook = uncaught_thread_hook(handler, previous_thread_hook)
    try:
        yield handler
    finally:
        handler.close()
        sys.excepthook = previous_hook
        threading.excepthook = previous_thread_hook
        for each in installed:
            root.removeHandler(each)
        root.setLevel(level)


def main(
    argv: list[str] | None = None,
    *,
    runs: Runs | None = None,
    run_readers: RunReaders | None = None,
) -> None:
    """Hold this server's registry id for its lifetime and serve its run."""
    with narration_installed():
        _serve(argv, runs, run_readers)


def _serve(argv: list[str] | None, runs: Runs | None, run_readers: RunReaders | None) -> None:
    arguments = sys.argv[1:] if argv is None else argv
    identity = _identity(arguments)
    root = instance_root(os.environ, os.getuid())
    clock = SystemClock()
    # A read-only server (``--reopen``) is a reader: it registers apart from the runs.
    directory = INSTANCES_DIRECTORY if identity.reopen is None else READERS_DIRECTORY
    registry = LiveRegistry(FileInstanceStore(root, directory=directory), clock)
    with registry.register(identity.instance_id) as hold:
        publisher = RecordPublisher(
            hold,
            _starting_record(identity, root, clock),
            clock,
            metadata=FileInstanceMetadataStore(root),
            threads=OsThreads(),
        )
        try:
            import_module("entrypoints.server").main(
                arguments, observer=publisher, runs=runs, run_readers=run_readers
            )
        finally:
            publisher.end()


if __name__ == "__main__":
    main()
