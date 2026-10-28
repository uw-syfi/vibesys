"""Project journal observations into typed store records without performing I/O.

``map_event`` returns a new state plus records and immutable content writes. The
caller persists content before records. State is replayable from the authoritative
JSONL journal; store sequences count records, never journal observations. Unknown
event kinds or invalid records raise rather than silently losing observations.

Builtin span attrs use their concrete proto contracts. Custom kinds whose legacy
journal has only JSON use a declared google.protobuf.StringValue retaining that
JSON exactly. Oversized strings are named blob attachments; their inline value
is ``blob:<sha256>`` and the attachment role identifies the original field path.
Unmatched historical workstream and adoption projections are retained in an
UNATTRIBUTED_EVENT notice's typed detail; they never invent extra work units.
Anonymous legacy controls use ``journal:<source sequence>`` as a local identity.
"""

from vibesys.records._activity import ACTIVITY_INTERVAL
from vibesys.records._lifecycle import epoch_header
from vibesys.records._mapping import DISPOSITIONS, map_event
from vibesys.records._types import BlobWrite, MappedEvent, MappingState, StreamWrite

__all__ = [
    "ACTIVITY_INTERVAL",
    "DISPOSITIONS",
    "BlobWrite",
    "MappedEvent",
    "MappingState",
    "StreamWrite",
    "epoch_header",
    "map_event",
]
