"""Test support for vs_logs: the store contract suite and its owner model.

``MemoryLogStore`` (in ``vs_logs.api``) is the Fake; every implementation subclasses
:class:`LogStoreContract`.
"""

from vs_logs._contracts import (
    CONTRACT_OWNER,
    ContractOwner,
    LogStoreContract,
    LogStoreUnderTest,
    bytes_by_pipe,
    read_everything,
)

__all__ = [
    "CONTRACT_OWNER",
    "ContractOwner",
    "LogStoreContract",
    "LogStoreUnderTest",
    "bytes_by_pipe",
    "read_everything",
]
