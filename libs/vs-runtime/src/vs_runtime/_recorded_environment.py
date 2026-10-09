"""Migration of run environments recorded by earlier releases."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vs_project.api import RunEnvironmentRecord


def migrate_recorded_run_environment(record: RunEnvironmentRecord) -> RunEnvironmentRecord:
    """Return the record a resumed run continues with.

    The host agent environment (``"local"``) no longer exists: agents always run
    in Docker. A run recorded with it continues as a Docker run; every other
    record is returned unchanged.
    """
    if record.name == "local":
        return record.model_copy(update={"name": "docker"})
    return record
