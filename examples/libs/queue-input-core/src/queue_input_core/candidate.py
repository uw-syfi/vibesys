from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def load_candidate(*, required: bool = True) -> Any | None:
    workspace = Path.cwd()
    if str(workspace) not in sys.path:
        sys.path.insert(0, str(workspace))

    try:
        from main import VibeServeQueue
    except ImportError as exc:
        if required:
            raise RuntimeError("Could not import VibeServeQueue from main.py") from exc
        return None
    return VibeServeQueue
