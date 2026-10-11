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
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING

from entrypoints.launcher import source_checkout_root
from server.host_api import (
    FailureReason,
    InstanceStatus,
    LiveInstanceRecord,
    RunPhase,
    RunStatus,
    StartupStage,
)
from server.registry import (
    FileInstanceStore,
    InstanceHold,
    LiveRegistry,
    host_facts,
    instance_log_path,
    instance_root,
    parse_instance_id,
)
from vs_project.api import ProjectStateError, validate_run_id
from vs_sim.api import SystemClock

if TYPE_CHECKING:
    from vs_sim.api import Clock

SERVER_EXITED = FailureReason(
    code="server_exited",
    stage="server",
    message="The server exited before its run ended; see its log.",
)
"""The reason a tombstone records when the run never reached an ended phase."""


class RecordPublisher:
    """Keep a held instance's record current, and leave it as the tombstone.

    The record's phase is folded here (``RunPhase.advance``) from the facts the
    server reports; ``end`` writes the final record.
    """

    def __init__(self, hold: InstanceHold, record: LiveInstanceRecord, clock: Clock) -> None:
        """Publish ``record`` through ``hold`` now; ``clock`` stamps the tombstone."""
        self._hold = hold
        self._record = record
        self._clock = clock
        hold.publish(record)

    def listening(self) -> None:
        """The control socket accepts connections."""
        self._publish(status=InstanceStatus.SERVING)

    def observe(
        self, *, status: RunStatus | None = None, failure: FailureReason | None = None
    ) -> None:
        """Fold one lifecycle fact into the record's phase, publishing a change."""
        phase = self._record.phase.advance(status=status, failure=failure)
        if phase != self._record.phase:
            self._publish(phase=phase)

    def end(self) -> None:
        """Write the tombstone: the phase the run ended in, or ``SERVER_EXITED``."""
        self.observe(failure=SERVER_EXITED)
        self._publish(ended_at=self._clock.now())

    def _publish(self, **update: object) -> None:
        self._record = LiveInstanceRecord.model_validate({**self._record.model_dump(), **update})
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


def main(argv: list[str] | None = None) -> None:
    """Hold this server's registry id for its lifetime and serve its run."""
    arguments = sys.argv[1:] if argv is None else argv
    identity = _identity(arguments)
    root = instance_root(os.environ, os.getuid())
    clock = SystemClock()
    with LiveRegistry(FileInstanceStore(root), clock).register(identity.instance_id) as hold:
        publisher = RecordPublisher(hold, _starting_record(identity, root, clock), clock)
        try:
            import_module("entrypoints.server").main(arguments, observer=publisher)
        finally:
            publisher.end()


if __name__ == "__main__":
    main()
