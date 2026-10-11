"""Filesystem-capability tests for experiment-chat transcripts."""

from __future__ import annotations

import errno
import json
import os
from typing import TYPE_CHECKING

import pytest

from server.chat.transcript import FileChatTranscriptStore

if TYPE_CHECKING:
    from pathlib import Path


def test_late_thread_symlink_preserves_the_open_error_and_root_capability(tmp_path: Path) -> None:
    root = tmp_path / "chat"
    transcripts = FileChatTranscriptStore.open(root)
    transcript = transcripts.open_transcript("thread-1")
    transcripts.close()
    outside = tmp_path / "outside"
    outside.mkdir()
    threads = root / "threads"
    threads.mkdir()
    thread = threads / "thread-1"
    thread.symlink_to(outside, target_is_directory=True)

    with pytest.raises(
        OSError, match=r"Too many levels of symbolic links|Not a directory"
    ) as error:
        transcript.append_exchange("question", "answer")

    assert error.value.errno in {errno.ELOOP, errno.ENOTDIR}
    thread.unlink()
    transcript.append_exchange("question", "answer")
    assert not list(outside.iterdir())
    assert (thread / "conversation.jsonl").is_file()
    transcript.close()


def test_history_read_retains_namespace_authority_after_path_replacement(tmp_path: Path) -> None:
    root = tmp_path / "chat"
    retained_root = tmp_path / "retained-chat"
    outside = tmp_path / "outside"
    transcripts = FileChatTranscriptStore.open(root)
    transcript = transcripts.open_transcript("thread-1")
    transcript.append_exchange("trusted question", "trusted answer")
    root.rename(retained_root)
    outside_thread = outside / "threads" / "thread-1"
    outside_thread.mkdir(parents=True)
    (outside_thread / "conversation.jsonl").write_text(
        json.dumps({"question": "outside", "answer": "outside"}) + "\n",
        encoding="utf-8",
    )
    root.symlink_to(outside, target_is_directory=True)

    history = transcript.read_history()

    assert json.loads(history) == {
        "question": "trusted question",
        "answer": "trusted answer",
    }
    transcript.close()
    transcripts.close()


def test_history_read_is_bounded_to_complete_recent_exchanges(tmp_path: Path) -> None:
    transcripts = FileChatTranscriptStore.open(tmp_path / "chat")
    transcript = transcripts.open_transcript(None)
    transcript.append_exchange("old", "x" * (64 * 1024))
    transcript.append_exchange("recent", "answer")

    history = transcript.read_history()

    assert "old" not in history
    assert json.loads(history) == {"question": "recent", "answer": "answer"}
    assert len(history.encode()) <= 64 * 1024
    transcript.close()
    transcripts.close()


def test_transcript_fifo_is_opened_nonblocking_and_rejected_for_read_and_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "chat"
    transcripts = FileChatTranscriptStore.open(root)
    transcript = transcripts.open_transcript(None)
    os.mkfifo(root / "conversation.jsonl")

    with pytest.raises(ValueError, match="must be a regular file"):
        transcript.read_history()
    with pytest.raises(ValueError, match="must be a regular file"):
        transcript.append_exchange("question", "answer")

    transcript.close()
    transcripts.close()


def test_transcript_directory_is_rejected_as_a_non_regular_entry(tmp_path: Path) -> None:
    root = tmp_path / "chat"
    transcripts = FileChatTranscriptStore.open(root)
    transcript = transcripts.open_transcript(None)
    (root / "conversation.jsonl").mkdir()

    with pytest.raises(ValueError, match="must be a regular file"):
        transcript.read_history()
    with pytest.raises(OSError, match="Is a directory"):
        transcript.append_exchange("question", "answer")

    transcript.close()
    transcripts.close()


def test_history_drops_an_unterminated_record_at_a_utf8_boundary(tmp_path: Path) -> None:
    root = tmp_path / "chat"
    transcripts = FileChatTranscriptStore.open(root)
    transcript = transcripts.open_transcript(None)
    complete = json.dumps(
        {"question": "trusted", "answer": "snowman ☃"}, ensure_ascii=False
    ).encode()
    partial = b'{"question":"unfinished","answer":"' + "💥".encode()[:2]
    (root / "conversation.jsonl").write_bytes(complete + b"\n" + partial)

    history = transcript.read_history()

    assert json.loads(history) == {"question": "trusted", "answer": "snowman ☃"}
    transcript.close()
    transcripts.close()


def test_history_ignores_a_sole_unterminated_record(tmp_path: Path) -> None:
    root = tmp_path / "chat"
    transcripts = FileChatTranscriptStore.open(root)
    transcript = transcripts.open_transcript(None)
    (root / "conversation.jsonl").write_text('{"question":"unfinished"}', encoding="utf-8")

    assert transcript.read_history() == ""

    transcript.close()
    transcripts.close()
