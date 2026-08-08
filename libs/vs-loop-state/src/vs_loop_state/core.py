"""Atomic JSON file persistence primitives shared by loop-state stores.

Framework-neutral: no knowledge of any particular loop's schema lives here.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


def atomic_write_json(path: Path, payload: Any) -> None:  # noqa: ANN401  # tracked: #288
    """Write *payload* to *path* as indented JSON, atomically.

    Writes to a sibling ``.tmp`` file first and ``os.replace``s it onto the
    target, so a process killed mid-write cannot leave a truncated file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)  # noqa: PTH105  # tracked: #288


def read_json(path: Path) -> Any | None:  # noqa: ANN401  # tracked: #288
    """Return the parsed JSON at *path*, or ``None`` if it doesn't exist."""
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
