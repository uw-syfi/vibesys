"""An in-memory :class:`~vs_sim.byte_source.ByteSource` a test grows, shrinks and replaces."""

from __future__ import annotations

from vs_sim.byte_source import SourceWatch, check_read


class FakeByteSource:
    """Bytes in memory that a test changes the way a writer changes a file.

    It reports a shrink or a replacement exactly as :class:`~vs_sim.byte_source.LocalFileSource`
    does, through the same :class:`~vs_sim.byte_source.SourceWatch`.
    """

    def __init__(self, data: bytes = b"", *, where: str = "fake source") -> None:
        """Start holding ``data``."""
        self._data = bytearray(data)
        self._generation = 0
        self._watch = SourceWatch(where)

    def append(self, data: bytes) -> None:
        """Grow the source, as a writer appending to its log."""
        self._data += data

    def truncate(self, size: int) -> None:
        """Cut the source to its first ``size`` bytes, as a writer truncating in place."""
        del self._data[size:]

    def replace(self, data: bytes) -> None:
        """Put other bytes in its place, as a writer replacing the file with a new one."""
        self._data = bytearray(data)
        self._generation += 1

    def size(self) -> int:
        """How many bytes it holds now."""
        self._watch.observe(self._generation, len(self._data))
        return len(self._data)

    def read(self, offset: int, max_bytes: int) -> bytes:
        """The bytes from ``offset``, at most ``max_bytes`` of them."""
        check_read(offset, max_bytes)
        self._watch.observe(self._generation, len(self._data))
        return bytes(self._data[offset : offset + max_bytes])
