"""Both store implementations pass the store contract, and a stream passes the sink contract."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vs_logs.api import FileLogStore, LogLimits, MemoryLogStore
from vs_logs.api.testing import (
    CONTRACT_OWNER,
    ContractOwner,
    LogStoreContract,
    LogStoreUnderTest,
    bytes_by_pipe,
    no_warning,
    read_everything,
)
from vs_logs.read import open_log_directory, reader_of
from vs_sim.api.testing import OutputSinkContract, OutputSinkUnderTest, SimThreads

if TYPE_CHECKING:
    from pathlib import Path

    from vs_logs.api import LogStore
    from vs_logs.read import LogReader


class TestFileLogStore(LogStoreContract):
    @pytest.fixture(autouse=True)
    def _directory(self, tmp_path_factory: pytest.TempPathFactory) -> None:
        self._factory = tmp_path_factory

    def store_under_test(self, limits: LogLimits) -> LogStoreUnderTest:
        directory = self._factory.mktemp("logs")
        threads = SimThreads()

        def store() -> LogStore[ContractOwner]:
            return FileLogStore(
                directory, CONTRACT_OWNER, threads=threads, warn=no_warning, limits=limits
            )

        return LogStoreUnderTest(
            store=store(), reader=open_log_directory(directory, CONTRACT_OWNER), reopen=store
        )


class TestMemoryLogStore(LogStoreContract):
    def store_under_test(self, limits: LogLimits) -> LogStoreUnderTest:
        threads = SimThreads()
        store = MemoryLogStore(CONTRACT_OWNER, threads=threads, warn=no_warning, limits=limits)

        def reopen() -> LogStore[ContractOwner]:
            return MemoryLogStore(
                CONTRACT_OWNER,
                threads=threads,
                warn=no_warning,
                limits=limits,
                volume=store.memory_volume,
            )

        return LogStoreUnderTest(
            store=store, reader=reader_of(store, CONTRACT_OWNER), reopen=reopen
        )


def _sink_under_test(
    store: LogStore[ContractOwner], reader: LogReader[ContractOwner]
) -> OutputSinkUnderTest:
    stream = store.open(ContractOwner(name="sink"), "cmd")
    return OutputSinkUnderTest(
        sink=stream,
        observe=lambda: bytes_by_pipe(read_everything(reader, stream.stream_id, 64)),
    )


class TestMemoryStreamIsAnOutputSink(OutputSinkContract):
    def sink_under_test(self) -> OutputSinkUnderTest:
        store = MemoryLogStore(
            CONTRACT_OWNER, threads=SimThreads(), warn=no_warning, limits=LogLimits()
        )
        return _sink_under_test(store, reader_of(store, CONTRACT_OWNER))


class TestFileStreamIsAnOutputSink(OutputSinkContract):
    @pytest.fixture(autouse=True)
    def _directory(self, tmp_path_factory: pytest.TempPathFactory) -> None:
        self._factory = tmp_path_factory

    def sink_under_test(self) -> OutputSinkUnderTest:
        directory: Path = self._factory.mktemp("sink")
        store = FileLogStore(directory, CONTRACT_OWNER, threads=SimThreads(), warn=no_warning)
        return _sink_under_test(store, open_log_directory(directory, CONTRACT_OWNER))
