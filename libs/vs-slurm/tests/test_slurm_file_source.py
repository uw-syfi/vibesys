"""A cluster file read live through the runner's transport behaves as every ByteSource does."""

from __future__ import annotations

import tempfile
from pathlib import Path, PurePosixPath

import pytest

from vs_sim.api import ByteSourceChangedError, SourceChange
from vs_sim.api.testing import ByteSourceContract, ByteSourceUnderTest, ManualClock
from vs_slurm.api import (
    MAX_REMOTE_READ_BYTES,
    FakeConnector,
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmJobRunner,
)


def _runner(root: Path) -> SlurmJobRunner:
    config = SlurmConfig(
        name="fake",
        remote_workspace_root=str(root / "remote"),
        transport=SlurmConnectorTransport(kind="connector", command=("fake-connector",)),
    )
    return SlurmJobRunner(config, processes=FakeConnector(root / "cluster"), clock=ManualClock())


def _append(path: Path, data: bytes) -> None:
    with path.open("ab") as file:
        file.write(data)


def _truncate(path: Path, size: int) -> None:
    with path.open("r+b") as file:
        file.truncate(size)


def _replace(path: Path, data: bytes) -> None:
    fresh = path.with_name(f"{path.name}.new")
    fresh.write_bytes(data)
    fresh.replace(path)


class TestSlurmFileSource(ByteSourceContract):
    """The contract, over the Fake connector's remote filesystem (this host's)."""

    @pytest.fixture(autouse=True)
    def _directory(self, tmp_path: Path) -> None:
        self._runner = _runner(tmp_path)
        self._root = tmp_path

    def source_under_test(self, initial: bytes) -> ByteSourceUnderTest:
        path = Path(tempfile.mkdtemp(dir=self._root)) / "stdout.txt"
        path.write_bytes(initial)
        return ByteSourceUnderTest(
            self._runner.remote_file(PurePosixPath(path)),
            append=lambda data: _append(path, data),
            truncate=lambda size: _truncate(path, size),
            replace=lambda data: _replace(path, data),
        )


def test_a_file_that_is_not_there_yet_is_missing_until_it_appears(tmp_path: Path) -> None:
    path = tmp_path / "late.txt"
    source = _runner(tmp_path).remote_file(PurePosixPath(path))

    with pytest.raises(FileNotFoundError):
        source.read(0, 4)
    path.write_bytes(b"late")

    assert source.read(0, 4) == b"late"


def test_a_file_removed_after_it_was_read_is_a_replacement(tmp_path: Path) -> None:
    path = tmp_path / "gone.txt"
    path.write_bytes(b"seen")
    source = _runner(tmp_path).remote_file(PurePosixPath(path))
    assert source.size() == len(b"seen")

    path.unlink()

    with pytest.raises(ByteSourceChangedError) as raised:
        source.size()
    assert raised.value.change is SourceChange.REPLACED


def test_one_read_returns_at_most_the_transport_bounded_size(tmp_path: Path) -> None:
    path = tmp_path / "big.txt"
    data = bytes(range(256)) * (MAX_REMOTE_READ_BYTES // 256 + 3)
    path.write_bytes(data)
    source = _runner(tmp_path).remote_file(PurePosixPath(path))

    first = source.read(0, len(data))

    assert first == data[:MAX_REMOTE_READ_BYTES]
    assert source.read(len(first), len(data)) == data[len(first) :]
