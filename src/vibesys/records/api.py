"""Project journal observations into typed store records without performing I/O.

``map_event`` returns a new state plus records and immutable content writes. The
caller persists content before records. State is replayable from the authoritative
JSONL journal; store sequences count records, never journal observations. Unknown
event kinds or invalid records raise rather than silently losing observations.
"""

from vibesys.records._mapping import DISPOSITIONS, map_event
from vibesys.records._types import BlobWrite, MappedEvent, MappingState, StreamWrite

__all__ = ["DISPOSITIONS", "BlobWrite", "MappedEvent", "MappingState", "StreamWrite", "map_event"]
