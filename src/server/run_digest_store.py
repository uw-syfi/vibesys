"""Read a committed native digest through the store library's semantic boundary.

Public surface: ``read_digest``. The run's committed prefix is folded once from its nearest
keyframe, so a read costs the records after it, never the log. Only the head's published prefix is
visible, and the skeleton passes through the shared retained-store upgrade.
"""

from pathlib import Path

from server.host_api import RunDigest
from server.run_digest import digest_store
from vs_proto.api import ValidationError
from vs_runlog.api import RunLogDamagedError, read_committed_prefix


def read_digest(path: Path) -> RunDigest:
    """Project one coherent prefix without opening a writer or changing retained bytes."""
    try:
        prefix = read_committed_prefix(path)
    except (ValidationError, RunLogDamagedError) as exc:
        message = f"{path}: invalid committed store"
        raise ValueError(message) from exc
    return digest_store(prefix.head, prefix.skeleton, last_event_at=prefix.last_record_at)
