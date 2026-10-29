"""Test support for vs_logs: the store contract suite and its owner model.

``MemoryLogStore`` (in ``vs_logs.api``) is the Fake; every implementation subclasses
:class:`LogStoreContract`. :class:`MemoryVolume` is the Fake's memory, which a test gives
a ``capacity`` to fill the disk under a store (``MemoryLogStore(volume=...)``).
``no_warning`` is the ``warn`` of a store whose volume never fails.
"""

from vs_logs._contracts import (
    CONTRACT_OWNER,
    ContractOwner,
    LogStoreContract,
    LogStoreUnderTest,
    bytes_by_pipe,
    no_warning,
    read_everything,
)
from vs_logs._volume import MemoryVolume

__all__ = [
    "CONTRACT_OWNER",
    "ContractOwner",
    "LogStoreContract",
    "LogStoreUnderTest",
    "MemoryVolume",
    "bytes_by_pipe",
    "no_warning",
    "read_everything",
]
