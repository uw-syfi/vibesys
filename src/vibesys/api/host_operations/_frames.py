"""The length prefix of one watch frame, shared by every watch."""


def frame(payload: bytes) -> bytes:
    """One length-delimited payload, at most 1 MiB."""
    if len(payload) > _MAX_BYTES:
        message = "watch frame exceeds 1 MiB"
        raise ValueError(message)
    return len(payload).to_bytes(4, "big") + payload


_MAX_BYTES = 1 << 20
