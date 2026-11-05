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


def _plain(value: object, path: str, depth: int) -> object:  # noqa: C901, PLR0911 - one branch per JSON kind
    if depth > _MAX_DEPTH:
        msg = f"dataset{path}: nested deeper than {_MAX_DEPTH} levels"
        raise DatasetError(msg)
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            msg = f"dataset{path}: {value!r} is not a finite number"
            raise DatasetError(msg)
        return value
    if isinstance(value, Enum):
        return _plain(value.value, path, depth + 1)
    if isinstance(value, bytes | bytearray):
        return base64.b64encode(bytes(value)).decode()
    if isinstance(value, datetime.date | datetime.datetime | datetime.time):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _plain(getattr(value, f.name), f"{path}.{f.name}", depth + 1)
            for f in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {str(k): _plain(v, f"{path}[{k!r}]", depth + 1) for k, v in value.items()}
    if isinstance(value, set | frozenset):
        items = [_plain(v, f"{path}[]", depth + 1) for v in value]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, list | tuple):
        return [_plain(v, f"{path}[{n}]", depth + 1) for n, v in enumerate(value)]
    if hasattr(value, "__dict__"):
        return {
            k: _plain(v, f"{path}.{k}", depth + 1)
            for k, v in vars(value).items()
            if not k.startswith("_")
        }
    msg = f"dataset{path}: a {type(value).__name__} cannot be written to the dataset file"
    raise DatasetError(msg)


def encode(data: object) -> bytes:
    """The dataset file's bytes."""
    return json.dumps(_plain(data, "", 0), allow_nan=False, separators=(",", ":")).encode() + b"\n"


def digest(encoded: bytes) -> str:
    """The dataset's digest: SHA-256 of the file, hex."""
    return hashlib.sha256(encoded).hexdigest()
