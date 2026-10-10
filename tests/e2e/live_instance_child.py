"""A child process that holds one live-registry id until its stdin closes."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from server.instances import (
    FileInstanceStore,
    InstanceStatus,
    LiveInstanceRecord,
    LiveRegistry,
    host_facts,
    instance_root,
    instance_socket_path,
)


def main(instance_id: str) -> None:
    """Register ``instance_id``, report readiness, and hold it until stdin closes."""
    root = instance_root(os.environ, os.getuid())
    hostname, version = host_facts()
    with LiveRegistry(FileInstanceStore(root)).register(instance_id) as hold:
        hold.publish(
            LiveInstanceRecord(
                id=instance_id,
                status=InstanceStatus.SERVING,
                socket_path=str(instance_socket_path(root, instance_id)),
                project_root=str(Path.cwd()),
                pid=os.getpid(),
                started_at=0.0,
                hostname=hostname,
                vibesys_version=version,
            )
        )
        sys.stdout.write("ready\n")
        sys.stdout.flush()
        sys.stdin.read()


if __name__ == "__main__":
    main(sys.argv[1])
