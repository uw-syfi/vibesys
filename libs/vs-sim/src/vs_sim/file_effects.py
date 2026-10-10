"""Real, deterministic interruptions of file writes without replacing filesystem code."""

from __future__ import annotations

import resource
import signal
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def file_size_limit(maximum: int) -> Iterator[None]:
    """Fail a write after at most ``maximum`` bytes, then restore process limits."""
    previous_limit = resource.getrlimit(resource.RLIMIT_FSIZE)
    previous_handler = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (maximum, previous_limit[1]))
        yield
    finally:
        try:
            resource.setrlimit(resource.RLIMIT_FSIZE, previous_limit)
        finally:
            signal.signal(signal.SIGXFSZ, previous_handler)
