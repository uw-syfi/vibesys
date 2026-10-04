"""Atomic state publication under deterministic filesystem interruptions."""

from __future__ import annotations

import io
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_project.api import ProjectStateError, atomic_write_bytes

if TYPE_CHECKING:
    from collections.abc import Buffer, Iterator

    from vs_project.api import AtomicWriteStream


class _InterruptedWriteError(OSError):
    """A scheduled filesystem interruption."""


class _MemoryStream(io.BytesIO):
    def __init__(self, filesystem: _FakeAtomicFilesystem, path: Path) -> None:
        super().__init__()
        self.filesystem = filesystem
        self.path = path

    def write(self, contents: Buffer, /) -> int:
        self.filesystem.step()
        selected = bytes(contents)[: self.filesystem.chunk_size]
        written = super().write(selected)
        self.filesystem.files[self.path] = self.getvalue()
        return written

    def flush(self) -> None:
        self.filesystem.step()
        super().flush()

    def close(self) -> None:
        super().close()
        self.filesystem.step()


class _FakeAtomicFilesystem:
    """Single-file filesystem with atomic rename and ordinal interruption."""

    def __init__(self, old: bytes, *, interrupt: int | None, chunk_size: int) -> None:
        self.files = {Path("state.json"): old}
        self.interrupt = interrupt
        self.chunk_size = chunk_size
        self.ordinal = 0

    def step(self) -> None:
        self.ordinal += 1
        if self.ordinal == self.interrupt:
            raise _InterruptedWriteError

    @contextmanager
    def temporary(self, destination: Path) -> Iterator[tuple[Path, AtomicWriteStream]]:
        self.step()
        temporary = destination.with_suffix(".tmp")
        assert temporary not in self.files
        self.files[temporary] = b""
        with _MemoryStream(self, temporary) as stream:
            yield temporary, stream

    def sync_file(self, stream: AtomicWriteStream) -> None:
        assert not stream.closed
        self.step()

    def replace(self, temporary: Path, destination: Path) -> None:
        self.step()
        self.files[destination] = self.files.pop(temporary)

    def sync_directory(self, directory: Path) -> None:
        assert directory == Path()
        self.step()

    def remove_temporary(self, temporary: Path) -> None:
        self.step()
        self.files.pop(temporary, None)


@given(old=st.binary(max_size=64), new=st.binary(min_size=1, max_size=64))
@settings(max_examples=15)
def test_interruption_at_every_operation_preserves_complete_content(old: bytes, new: bytes) -> None:
    uninterrupted = _FakeAtomicFilesystem(old, interrupt=None, chunk_size=len(new))
    atomic_write_bytes(Path("state.json"), new, effects=uninterrupted)
    assert uninterrupted.files == {Path("state.json"): new}

    for interruption in range(1, uninterrupted.ordinal + 1):
        filesystem = _FakeAtomicFilesystem(old, interrupt=interruption, chunk_size=len(new))
        with pytest.raises(_InterruptedWriteError):
            atomic_write_bytes(Path("state.json"), new, effects=filesystem)
        assert filesystem.files[Path("state.json")] in (old, new)
        assert list(filesystem.files) == [Path("state.json")]


@given(
    contents=st.binary(min_size=1, max_size=64), chunk_size=st.integers(min_value=1, max_value=64)
)
@settings(max_examples=15)
def test_short_writes_publish_every_byte(contents: bytes, chunk_size: int) -> None:
    filesystem = _FakeAtomicFilesystem(b"old", interrupt=None, chunk_size=chunk_size)
    atomic_write_bytes(Path("state.json"), contents, effects=filesystem)
    assert filesystem.files == {Path("state.json"): contents}


def test_nonprogressing_write_preserves_old_content_and_cleans_staging() -> None:
    filesystem = _FakeAtomicFilesystem(b"old", interrupt=None, chunk_size=0)
    with pytest.raises(ProjectStateError, match="made no progress"):
        atomic_write_bytes(Path("state.json"), b"new", effects=filesystem)
    assert filesystem.files == {Path("state.json"): b"old"}
