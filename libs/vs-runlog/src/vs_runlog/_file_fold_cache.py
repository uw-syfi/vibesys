"""state.pb on disk: the file edge of the fold cache."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_runlog._atomic import FileSync, OsFileSync, publish
from vs_runlog._fold_cache import CachedFold, CacheMiss, decode_cache, encode_cache

if TYPE_CHECKING:
    from pathlib import Path


class FileFoldCache:
    """``state.pb`` on disk, replaced with a temporary file, an fsync and a rename."""

    def __init__(self, path: Path, *, sync: FileSync | None = None) -> None:
        """Cache in ``path``; ``sync`` is the durability operation, the operating system's by default."""
        self._path = path
        self._sync = OsFileSync() if sync is None else sync

    def load(self) -> CachedFold | CacheMiss:
        """The file as stored; a missing file is ``ABSENT``, anything else unusable ``UNREADABLE``."""
        try:
            return decode_cache(self._path.read_bytes())
        except FileNotFoundError:
            return CacheMiss.ABSENT
        except OSError:
            return CacheMiss.UNREADABLE

    def store(self, cached: CachedFold) -> None:
        """Replace the file with ``cached``."""
        publish(self._path, encode_cache(cached), sync=self._sync)
