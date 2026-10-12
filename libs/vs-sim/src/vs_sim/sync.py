"""Real durability synchronization behind the public simulation boundary."""

from __future__ import annotations

import os


def sync_descriptor(descriptor: int) -> None:
    """Synchronize file data and metadata for an open descriptor."""
    os.fsync(descriptor)
