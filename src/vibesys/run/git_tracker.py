"""Re-export of Git history tracking, now owned by ``vs_project``.

Kept so core-internal importers of ``vibesys.run.git_tracker`` keep working.
New code should import ``GitTracker`` from ``vs_project`` directly.
"""

from __future__ import annotations

from vs_project import FrameworkSnapshotStatus, GitTracker

__all__ = [
    "FrameworkSnapshotStatus",
    "GitTracker",
]
