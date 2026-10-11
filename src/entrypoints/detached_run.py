"""A detached run server's first step: publish its registry record, then load the engine.

``vibesys --detach`` allocates the instance id and the run id, then starts
``python -m entrypoints.detached_run`` with both. This module takes the
instance's lifetime lock and publishes a record that names the run before it
imports anything heavy, so ``vibesys instances list`` shows the run with its id
within the interpreter's start-up time, not after the engine's (about 1.7 s)
or the run's setup. It then serves the run through ``entrypoints.server``,
which reports its milestones to the ``RecordPublisher`` held here.

This module is light: ``tests/architecture/test_light_entrypoints.py`` holds
its module-level imports to the same allowlist as the host commands.
"""

from __future__ import annotations

import argparse
import os
import sys
from importlib import import_module
from pathlib import Path

from entrypoints.launcher import source_checkout_root
from server.host_api import InstanceStatus, LiveInstanceRecord
from server.registry import (
    FileInstanceStore,
    InstanceHold,
    LiveRegistry,
    host_facts,
    instance_root,
    parse_instance_id,
)
from vs_project.api import ProjectStateError, validate_run_id
from vs_sim.api import SystemClock


class RecordPublisher:
    """Keep a held instance's record current as its server reaches milestones."""

    def __init__(self, hold: InstanceHold, record: LiveInstanceRecord) -> None:
        """Publish ``record`` through ``hold`` now."""
        self._hold = hold
        self._record = record
        hold.publish(record)

    def listening(self) -> None:
        """The control socket accepts connections."""
        self._publish(self._record.model_copy(update={"status": InstanceStatus.SERVING}))

    def _publish(self, record: LiveInstanceRecord) -> None:
        self._record = LiveInstanceRecord.model_validate(record.model_dump())
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


def _starting_record(identity: argparse.Namespace) -> LiveInstanceRecord:
    hostname, version = host_facts()
    checkout = source_checkout_root()
    return LiveInstanceRecord(
        id=identity.instance_id,
        status=InstanceStatus.STARTING,
        socket_path=str(identity.control_socket),
        project_root=str(Path.cwd().resolve()),
        run_id=identity.run_id,
        pid=os.getpid(),
        started_at=SystemClock().now(),
        hostname=hostname,
        vibesys_version=version,
        vibesys_root=None if checkout is None else str(checkout),
    )


def main(argv: list[str] | None = None) -> None:
    """Hold this server's registry id for its lifetime and serve its run."""
    arguments = sys.argv[1:] if argv is None else argv
    identity = _identity(arguments)
    root = instance_root(os.environ, os.getuid())
    with LiveRegistry(FileInstanceStore(root)).register(identity.instance_id) as hold:
        publisher = RecordPublisher(hold, _starting_record(identity))
        import_module("entrypoints.server").main(arguments, observer=publisher)


if __name__ == "__main__":
    main()
