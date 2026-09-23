"""Re-export of run logging, now owned by ``vs_project``.

Kept so core-internal importers of ``vibesys.run.logger`` keep working. New
code should import ``RunLogger`` from ``vs_project`` directly.
"""

from __future__ import annotations

from vs_project.api import RunLogger, strip_ansi

__all__ = [
    "RunLogger",
    "strip_ansi",
]
