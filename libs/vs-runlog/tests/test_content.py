"""Content writes are exact, immutable and safe to replay after interruption."""

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_runlog.api import FileRunContent


@given(chunks=st.lists(st.binary(max_size=1024), max_size=10))
def test_blobs_and_stream_appends_are_lossless_and_idempotent(chunks: list[bytes]) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        content = FileRunContent(root / "blobs", root / "streams")
        offset = 0
        for chunk in chunks:
            digest = hashlib.sha256(chunk).hexdigest()
            content.write_blob(digest, chunk)
            content.write_blob(digest, chunk)
            assert (root / "blobs" / digest).read_bytes() == chunk
            content.append_stream("transcript-a", offset, chunk)
            content.append_stream("transcript-a", offset, chunk)
            offset += len(chunk)
        if chunks:
            assert (root / "streams/transcript-a").read_bytes() == b"".join(chunks)


@given(payload=st.binary(min_size=1, max_size=128))
def test_conflicting_content_is_never_overwritten(payload: bytes) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        store = FileRunContent(root / "blobs", root / "streams")
        store.append_stream("stream-a", 0, payload)
        with pytest.raises(ValueError, match="conflicting"):
            store.append_stream("stream-a", 0, bytes([payload[0] ^ 1]) + payload[1:])
        assert (root / "streams/stream-a").read_bytes() == payload
        with pytest.raises(ValueError, match="digest"):
            store.write_blob("invalid", payload)
        with pytest.raises(ValueError, match="missing prefix"):
            store.append_stream("new-stream", 1, payload)


@pytest.mark.parametrize("name", ["../outside", "/absolute", "a/b", "", ".", "a" * 256 + "b"])
def test_stream_ids_cannot_escape_the_stream_directory(tmp_path: Path, name: str) -> None:
    content = FileRunContent(tmp_path / "blobs", tmp_path / "streams")
    with pytest.raises(ValueError, match="stream id"):
        content.append_stream(name, 0, b"data")
    assert list(tmp_path.iterdir()) == []
