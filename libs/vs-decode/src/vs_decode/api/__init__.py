"""Public interface of vs_decode: bounded, strict decoding of untrusted JSON.

``decode_json`` decodes bytes or text, and ``read_json`` reads a file at most one byte
past a size cap and decodes it, into a ``BaseModel`` that forbids unknown keys or a
``TypeAdapter`` target, under ``ValidationOptions`` (strict mode, context). Both return the value or an ``Unreadable`` (its
``UnreadableKind``, source and ``Problem`` key paths) and never raise for bad input.
Only store implementations and edge adapters, the modules that receive bytes from
outside the process, call them.
"""

from vs_decode._decode import (
    Problem,
    Unreadable,
    UnreadableKind,
    ValidationOptions,
    decode_json,
    read_json,
)

__all__ = [
    "Problem",
    "Unreadable",
    "UnreadableKind",
    "ValidationOptions",
    "decode_json",
    "read_json",
]
