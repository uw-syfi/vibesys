"""Progress beats from inside a GPU job, for the hang watchdog of ``gpu_job``.

``gpu_job.run`` sets ``MEGABENCH_HEARTBEAT`` to a file path before it starts the
agent's command. The harness calls ``beat`` after each finished candidate call
(and before long steps such as a build, with a longer ``allow``). The watchdog
stops the job when the file stops changing for longer than its idle limit.
Without the variable, ``beat`` does nothing.
"""

from __future__ import annotations

import json
import os
import time

ENV = "MEGABENCH_HEARTBEAT"
_last = 0.0


def beat(label: str, allow: float | None = None, *, force: bool = False) -> None:
    """Record progress. ``allow``: seconds the next step can take with no beat."""
    global _last
    path = os.environ.get(ENV)
    if not path:
        return
    now = time.monotonic()
    if not force and allow is None and now - _last < 1.0:
        return  # at most one write a second in tight loops
    _last = now
    try:
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump({"label": label, "allow": allow, "pid": os.getpid()}, f)
        os.replace(tmp, path)
    except OSError:
        pass


def read(path: str) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
