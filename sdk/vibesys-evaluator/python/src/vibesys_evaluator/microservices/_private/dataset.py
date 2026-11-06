"""The dataset file: the task's dataset as JSON, kept as an artifact and named by its digest.

``dataset()`` is a pure function of the task's constants, so every process
of a sample (the orchestrator, the seed Job, each phase pod) builds the same
value; the file is what other programs (accuracy oracles in another
language) read, and the digest is how a process proves it built the same
dataset as the orchestrator.

Encoding: dataclasses become objects of their fields in declaration order;
other objects with attributes become objects of their ``vars()``; tuples,
lists, sets (sorted) and frozensets become arrays; mapping keys become
strings; bytes become base64 strings (as Go's ``encoding/json`` reads
``[]byte``); dates their ISO text. Floats must be finite.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import hashlib
import json
import math
from collections.abc import Mapping
from enum import Enum

_MAX_DEPTH = 64


class DatasetError(ValueError):
    """The dataset holds a value the file cannot represent (the message gives its path)."""


_NOT_LEAF = object()


def _leaf(value: object, path: str) -> object:
    """A scalar's JSON value, or ``_NOT_LEAF``."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            msg = f"dataset{path}: {value!r} is not a finite number"
            raise DatasetError(msg)
        return value
    if isinstance(value, bytes | bytearray):
        return base64.b64encode(bytes(value)).decode()
    if isinstance(value, datetime.date | datetime.time):
        return value.isoformat()
    return _NOT_LEAF


def _plain(value: object, path: str, depth: int) -> object:
    if depth > _MAX_DEPTH:
        msg = f"dataset{path}: nested deeper than {_MAX_DEPTH} levels"
        raise DatasetError(msg)
    leaf = _leaf(value.value if isinstance(value, Enum) else value, path)
    if leaf is not _NOT_LEAF:
        return leaf
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields: dict[str, object] = {
            f.name: getattr(value, f.name) for f in dataclasses.fields(value)
        }
    elif isinstance(value, Mapping):
        fields = {str(k): v for k, v in value.items()}
    elif isinstance(value, set | frozenset):
        items = [_plain(v, f"{path}[]", depth + 1) for v in value]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True))
    elif isinstance(value, list | tuple):
        return [_plain(v, f"{path}[{n}]", depth + 1) for n, v in enumerate(value)]
    elif hasattr(value, "__dict__"):
        fields = {k: v for k, v in vars(value).items() if not k.startswith("_")}
    else:
        msg = f"dataset{path}: a {type(value).__name__} cannot be written to the dataset file"
        raise DatasetError(msg)
    return {k: _plain(v, f"{path}.{k}", depth + 1) for k, v in fields.items()}


def encode(data: object) -> bytes:
    """The dataset file's bytes."""
    return json.dumps(_plain(data, "", 0), allow_nan=False, separators=(",", ":")).encode() + b"\n"


def digest(encoded: bytes) -> str:
    """The dataset's digest: SHA-256 of the file, hex."""
    return hashlib.sha256(encoded).hexdigest()
