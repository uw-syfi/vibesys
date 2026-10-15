"""A child process that holds one live-registry id until its stdin closes."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from entrypoints.detached_run import RecordPublisher
from server.host_api import InstanceStatus, LiveInstanceRecord, RunPhase, RunStatus
from server.registry import (
    FileInstanceMetadataStore,
    FileInstanceStore,
    LiveRegistry,
    host_facts,
    instance_root,
    instance_socket_path,
)
from vs_sim.api import OsThreads, SystemClock


def main(instance_id: str) -> None:
    """Register ``instance_id``, report readiness, and run until stdin closes, then complete."""
    root = instance_root(os.environ, os.getuid())
    hostname, version = host_facts()
    clock = SystemClock()
    with LiveRegistry(FileInstanceStore(root), clock).register(instance_id) as hold:
        publisher = RecordPublisher(
            hold,
            LiveInstanceRecord(
                id=instance_id,
                status=InstanceStatus.SERVING,
                phase=RunPhase(status=RunStatus.RUNNING),
                log_path=str(Path.cwd() / "server.log"),
                socket_path=str(instance_socket_path(root, instance_id)),
                project_root=str(Path.cwd()),
                run_id=f"run-{instance_id}",
                pid=os.getpid(),
                started_at=0.0,
                hostname=hostname,
                vibesys_version=version,
            ),
            clock,
            metadata=FileInstanceMetadataStore(root),
            threads=OsThreads(),
        )
        sys.stdout.write("ready\n")
        sys.stdout.flush()
        sys.stdin.read()
        publisher.observe(status=RunStatus.COMPLETED)
        publisher.end()


if __name__ == "__main__":
    main(sys.argv[1])
