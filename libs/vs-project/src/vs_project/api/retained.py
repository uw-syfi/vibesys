"""Read-only access to retained run documents and their original store decoders."""

from vs_decode.api import Unreadable, decode_json
from vs_project._retained import RetainedFile
from vs_project._store_document import StoreDocument
from vs_project.api.state_store import StoreRecord

__all__ = ["RetainedFile", "RetainedStoreError", "decode_store"]


class RetainedStoreError(ValueError):
    """A retained document fails the original strict project-store schema."""


def decode_store(path: str, data: bytes) -> StoreRecord | None:
    """Decode both original inline-lease and split-lease stores without modifying either."""
    value = decode_json(data, StoreDocument, source=path, max_bytes=128 * 1024 * 1024)
    if isinstance(value, Unreadable):
        message = str(value)
        raise RetainedStoreError(message)
    return value.record
