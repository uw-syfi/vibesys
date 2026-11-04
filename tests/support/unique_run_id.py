"""A run id no other test on this host shares.

The reapers under test read the host's whole process list (``/proc``, or ``ps``), so the marks
a test gives its processes are a namespace shared with every concurrent test: another xdist
worker, another pytest session, another agent's run on the same machine. A fixed mark (``run-a``
with ``step-a``) is swept by whichever of them reaps that mark first, and the other finds its
process gone. A run id is part of every mark, so a test that takes its own run id owns its marks.
"""

from __future__ import annotations

import uuid


def unique_run_id(label: str = "run") -> str:
    """``label`` and a random suffix: a valid run id, holding no whitespace a mark refuses."""
    return f"{label}-{uuid.uuid4().hex[:12]}"
