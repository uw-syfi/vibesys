"""The read side: LocalFileSource and FakeByteSource pass one contract and read any offsets."""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sim.api import ByteSourceChangedError, LocalFileSource, SourceChange
from vs_sim.api.testing import ByteSourceContract, ByteSourceUnderTest, FakeByteSource

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vs_sim.api import ByteSource


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


class TestLocalFileSource(ByteSourceContract):
    @pytest.fixture(autouse=True)
    def _directory(self, tmp_path: Path) -> None:
        self._directory_path = tmp_path

    def source_under_test(self, initial: bytes) -> ByteSourceUnderTest:
        path = Path(tempfile.mkdtemp(dir=self._directory_path)) / "log"
        path.write_bytes(initial)
        return ByteSourceUnderTest(
            LocalFileSource(path),
            append=lambda data: _append(path, data),
            truncate=lambda size: _truncate(path, size),
            replace=lambda data: _replace(path, data),
        )


class TestFakeByteSource(ByteSourceContract):
    def source_under_test(self, initial: bytes) -> ByteSourceUnderTest:
        source = FakeByteSource(initial)
        return ByteSourceUnderTest(
            source, append=source.append, truncate=source.truncate, replace=source.replace
        )


@contextmanager
def _sources(content: bytes) -> Iterator[tuple[ByteSource, ...]]:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "log"
        path.write_bytes(content)
        yield LocalFileSource(path), FakeByteSource(content)


@given(
    content=st.binary(max_size=300),
    reads=st.lists(st.tuples(st.integers(0, 320), st.integers(1, 64)), max_size=20),
)
def test_reads_at_arbitrary_offsets_return_the_bytes_there(
    content: bytes, reads: list[tuple[int, int]]
) -> None:
    with _sources(content) as sources:
        for source in sources:
            for offset, max_bytes in reads:
                assert source.read(offset, max_bytes) == content[offset : offset + max_bytes]


@given(content=st.binary(max_size=300), steps=st.lists(st.integers(1, 40), min_size=1))
def test_a_reader_advancing_by_what_it_got_reassembles_the_source(
    content: bytes, steps: list[int]
) -> None:
    with _sources(content) as sources:
        for source in sources:
            offset, read, turn = 0, b"", 0
            while chunk := source.read(offset, steps[turn % len(steps)]):
                read += chunk
                offset += len(chunk)
                turn += 1
            assert read == content
            assert source.size() == len(content)


def test_a_file_never_seen_and_missing_is_not_found_and_once_seen_its_removal_is_reported(
    tmp_path: Path,
) -> None:
    path = tmp_path / "log"
    source = LocalFileSource(path)

    with pytest.raises(FileNotFoundError):
        source.size()
    path.write_bytes(b"started")
    assert source.read(0, 7) == b"started"
    path.unlink()

    with pytest.raises(ByteSourceChangedError) as raised:
        source.size()
    assert raised.value.change is SourceChange.REPLACED
    assert str(path) in str(raised.value)
