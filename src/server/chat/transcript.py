"""Filesystem capability for one experiment-chat transcript."""

from __future__ import annotations

import json
import os
import stat
from contextlib import suppress
from typing import TYPE_CHECKING

from server.events import validate_chat_thread_id

if TYPE_CHECKING:
    from pathlib import Path

    from server.events import ChatThreadId

_DIRECTORY_MODE = 0o700
_TRANSCRIPT_MODE = 0o600
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_TRANSCRIPT_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
_TRANSCRIPT_FLAGS = (
    os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
)
_TRANSCRIPT_HISTORY_MAX_BYTES = 64 * 1024


class FileChatTranscriptStore:
    """Retain the attached Project chat namespace as a directory capability."""

    def __init__(self, root_descriptor: int, root: Path) -> None:
        """Own the open chat root and its display path."""
        self._root_descriptor: int | None = root_descriptor
        self._root = root

    @classmethod
    def open(cls, root: Path) -> FileChatTranscriptStore:
        """Open the chat root once while its Project namespace is attached."""
        root.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
        try:
            descriptor = os.open(root, _DIRECTORY_FLAGS)
        except OSError as exc:
            message = f"experiment chat state directory is not a safe directory: {root}"
            raise ValueError(message) from exc
        return cls(descriptor, root)

    def open_transcript(self, thread_id: str | None) -> FileChatTranscript:
        """Derive one independently owned transcript from the retained root."""
        identity = None if thread_id is None else validate_chat_thread_id(thread_id)
        return FileChatTranscript(
            os.dup(self._require_open()),
            self._root,
            identity,
        )

    def close(self) -> None:
        """Release the retained Project namespace capability once."""
        descriptor, self._root_descriptor = self._root_descriptor, None
        if descriptor is not None:
            os.close(descriptor)

    def _require_open(self) -> int:
        descriptor = self._root_descriptor
        if descriptor is None:
            message = "experiment chat transcript store is closed"
            raise RuntimeError(message)
        return descriptor


class FileChatTranscript:
    """Append a transcript through a descriptor rooted in the Project namespace.

    The retained root descriptor is the authority. Descendants are opened
    relative to it without following links, so replacing a pathname after
    session construction cannot redirect a later write.
    """

    def __init__(
        self,
        root_descriptor: int,
        root: Path,
        thread_id: ChatThreadId | None,
    ) -> None:
        """Retain the trusted root descriptor and validated thread identity."""
        self._root_descriptor: int | None = root_descriptor
        self._root = root
        self._thread_id = thread_id
        try:
            self._reject_existing_redirect()
        except BaseException:
            self.close()
            raise

    def append_exchange(self, question: str, answer: str) -> None:
        """Append one exchange without resolving an ambient absolute pathname."""
        directory_descriptor = self._open_state_directory()
        try:
            transcript_descriptor = _open_regular_transcript(
                "conversation.jsonl",
                _TRANSCRIPT_FLAGS,
                dir_fd=directory_descriptor,
                mode=_TRANSCRIPT_MODE,
            )
            with os.fdopen(transcript_descriptor, "a", encoding="utf-8") as transcript:
                transcript.write(
                    json.dumps({"question": question, "answer": answer}, ensure_ascii=False) + "\n"
                )
        finally:
            os.close(directory_descriptor)

    def read_history(self) -> str:
        """Read a bounded complete-line tail through the retained namespace."""
        directory_descriptor = self._open_existing_state_directory()
        if directory_descriptor is None:
            return ""
        try:
            try:
                transcript_descriptor = _open_regular_transcript(
                    "conversation.jsonl",
                    _READ_TRANSCRIPT_FLAGS,
                    dir_fd=directory_descriptor,
                )
            except FileNotFoundError:
                return ""
            try:
                end = os.lseek(transcript_descriptor, 0, os.SEEK_END)
                offset = max(0, end - _TRANSCRIPT_HISTORY_MAX_BYTES)
                os.lseek(transcript_descriptor, offset, os.SEEK_SET)
                content = _read_up_to(transcript_descriptor, end - offset)
            finally:
                os.close(transcript_descriptor)
        finally:
            os.close(directory_descriptor)
        if offset:
            _, separator, content = content.partition(b"\n")
            if not separator:
                return ""
        final_record_end = content.rfind(b"\n")
        if final_record_end < 0:
            return ""
        return content[:final_record_end].decode("utf-8")

    def _open_state_directory(self) -> int:
        """Return one owned state-directory descriptor or transfer nothing."""
        root_descriptor = self._require_open()
        if self._thread_id is None:
            return os.dup(root_descriptor)
        threads_descriptor = _open_or_create_directory(root_descriptor, "threads")
        try:
            return _open_or_create_directory(threads_descriptor, self._thread_id)
        finally:
            os.close(threads_descriptor)

    def _open_existing_state_directory(self) -> int | None:
        """Return an existing state directory without creating path components."""
        root_descriptor = self._require_open()
        if self._thread_id is None:
            return os.dup(root_descriptor)
        threads_descriptor = _open_existing_directory(root_descriptor, "threads")
        if threads_descriptor is None:
            return None
        try:
            return _open_existing_directory(threads_descriptor, self._thread_id)
        finally:
            os.close(threads_descriptor)

    def close(self) -> None:
        """Release this transcript's retained namespace capability once."""
        descriptor, self._root_descriptor = self._root_descriptor, None
        if descriptor is not None:
            os.close(descriptor)

    def _reject_existing_redirect(self) -> None:
        """Give an early diagnostic for a path already replaced by a link.

        This check is only diagnostic. ``append_exchange`` independently uses
        descriptor-relative, no-follow operations for its security boundary.
        """
        if self._thread_id is None:
            return
        root_descriptor = self._require_open()
        try:
            threads_descriptor = _open_existing_directory(root_descriptor, "threads")
        except OSError as exc:
            raise self._unsafe_component_error("threads") from exc
        if threads_descriptor is None:
            return
        try:
            destination_descriptor = _open_existing_directory(threads_descriptor, self._thread_id)
            if destination_descriptor is not None:
                os.close(destination_descriptor)
        except OSError as exc:
            raise self._unsafe_component_error(f"threads/{self._thread_id}") from exc
        finally:
            os.close(threads_descriptor)

    def _require_open(self) -> int:
        descriptor = self._root_descriptor
        if descriptor is None:
            message = "experiment chat transcript is closed"
            raise RuntimeError(message)
        return descriptor

    def _unsafe_component_error(self, component: str) -> ValueError:
        identity = self._thread_id
        return ValueError(
            f"invalid experiment chat thread identity {identity!r}: transcript path "
            f"component {self._root / component} is a symlink or non-directory"
        )


def _open_existing_directory(parent_descriptor: int, name: str) -> int | None:
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
    except FileNotFoundError:
        return None


def _open_or_create_directory(parent_descriptor: int, name: str) -> int:
    with suppress(FileExistsError):
        os.mkdir(name, mode=_DIRECTORY_MODE, dir_fd=parent_descriptor)
    return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)


def _open_regular_transcript(
    name: str,
    flags: int,
    *,
    dir_fd: int,
    mode: int = 0o777,
) -> int:
    """Open one nonblocking transcript entry and transfer only a regular file."""
    descriptor = os.open(name, flags, mode, dir_fd=dir_fd)
    try:
        descriptor_mode = os.fstat(descriptor).st_mode
    except BaseException:
        os.close(descriptor)
        raise
    if not stat.S_ISREG(descriptor_mode):
        os.close(descriptor)
        message = f"experiment chat transcript entry {name!r} must be a regular file"
        raise ValueError(message)
    return descriptor


def _read_up_to(descriptor: int, maximum_bytes: int) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum_bytes
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
