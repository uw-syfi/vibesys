"""Frozen validation descriptors of retained positional run storage, shipped as package data.

``envelope-v<N>.json`` holds the shared framework descriptors (envelope, runtime record,
flow state, publications, operation result and agent sessions) as envelope version N
wrote them, generated from that release's models by ``tests/vibesys/fact_import/
descriptors.py``. ``retained.json`` holds the strategy, legacy and manifest descriptors
captured from code that no longer defines them. Tests pin every entry other than the
current envelope version by hash, so none is edited in place. Descriptions, titles and
defaults are stripped because validation does not use them.
"""

from __future__ import annotations

import json
import re
from functools import cache
from importlib.resources import files
from typing import Any

OLDEST_ENVELOPE_VERSION = 34
"""The oldest envelope version the newest frozen envelope descriptors still read."""

_DIRECTORY = files(__package__) / "descriptors"
_ENVELOPE_FILE = re.compile(r"envelope-v([1-9][0-9]*)\.json")


@cache
def envelope_versions() -> tuple[int, ...]:
    """Every envelope version with frozen descriptors, oldest first."""
    return tuple(
        sorted(
            int(match.group(1))
            for entry in _DIRECTORY.iterdir()
            if (match := _ENVELOPE_FILE.fullmatch(entry.name))
        )
    )


@cache
def _load(name: str) -> dict[str, Any]:
    return json.loads(_DIRECTORY.joinpath(name).read_bytes())


def descriptor(key: str, envelope_version: int | None) -> dict[str, Any]:
    """The descriptor for ``key``; an envelope is read by the first frozen version not older.

    A source without an envelope version, or newer than every frozen one, is read by the
    newest; the container check then rejects an unknown version. Raises ``KeyError`` for
    an unknown key.
    """
    retained = _load("retained.json")
    if key in retained:
        return retained[key]
    versions = envelope_versions()
    version = (
        versions[-1]
        if envelope_version is None
        else next((frozen for frozen in versions if frozen >= envelope_version), versions[-1])
    )
    return _load(f"envelope-v{version}.json")[key]
