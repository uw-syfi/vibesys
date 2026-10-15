"""A file on the cluster, read as a growing :class:`vs_sim.api.ByteSource` over the transport.

A running job's output files live only on the cluster until the job is collected. A
:class:`SlurmFileSource` reads one of them by offset through the runner's own transport (ssh,
the local shell, or a connector), so a caller can show a stage's output while it runs. It
opens no new kind of connection: each call is one framed remote command.

The command is the same for every transport and needs no pipe:

- ``od -A n -v -t x1 -j N -N M`` prints the bytes at an offset as hexadecimal, which a text
  transport carries unchanged whatever the bytes are. A byte takes about three characters,
  so a read of :data:`MAX_READ_BYTES` stays under the transport's one-MiB stream cap.
- ``stat -c %d:%i:%s`` after the read names the file's device, inode and size, so a
  replacement or a shrink is seen at the read that met it (``SourceWatch``).
- ``[ -f P ]`` tells a file that is not there yet from a transport that failed.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from vs_sim.api import SourceWatch, check_read

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import PurePosixPath

MAX_READ_BYTES = 256 * 1024
"""The most bytes one remote read returns: its hex form stays under the 1 MiB stream cap."""

_ABSENT = "absent"
_STAT_FIELDS = 3


class RemoteFileAnswerError(OSError):
    """The cluster's answer to a file read was not the shape the read command prints."""

    def __init__(self, where: str) -> None:
        """Name the file whose read came back malformed."""
        super().__init__(f"{where}: malformed read answer")


class SlurmFileSource:
    """One file on the cluster, read without holding it open between calls.

    ``read_remote`` runs one shell command on the cluster and returns its whole stdout, or
    raises ``SlurmError`` when the answer did not arrive whole; an answer that is not what
    the command prints raises :class:`RemoteFileAnswerError`. A file never seen that does
    not exist raises ``FileNotFoundError``, so a reader can wait for it to appear; once seen,
    its removal is a replacement.
    """

    def __init__(self, path: PurePosixPath, read_remote: Callable[[str], str]) -> None:
        """Read the cluster file at ``path`` through ``read_remote``."""
        self._path = shlex.quote(path.as_posix())
        self._read_remote = read_remote
        self._watch = SourceWatch(f"cluster file {path.as_posix()}")

    def size(self) -> int:
        """The file's size now."""
        _, size = self._run("")
        return size

    def read(self, offset: int, max_bytes: int) -> bytes:
        """The bytes at ``offset``, at most ``max_bytes`` and :data:`MAX_READ_BYTES` of them."""
        check_read(offset, max_bytes)
        count = min(max_bytes, MAX_READ_BYTES)
        # od fails (quietly) when asked to skip past the end; stat still answers.
        dump = f"od -A n -v -t x1 -j {offset} -N {count} {self._path} 2> /dev/null; "
        data, _ = self._run(dump)
        return data

    def _run(self, dump: str) -> tuple[bytes, int]:
        command = (
            f"if [ -f {self._path} ]; then {dump}stat -c %d:%i:%s {self._path}; "
            f"else printf '%s\\n' {_ABSENT}; fi"
        )
        lines = self._read_remote(command).splitlines()
        if lines == [_ABSENT]:
            self._watch.lost()
            raise FileNotFoundError(self._watch.where)
        if not lines:
            raise RemoteFileAnswerError(self._watch.where)
        fields = lines[-1].split(":")
        if len(fields) != _STAT_FIELDS:
            raise RemoteFileAnswerError(self._watch.where)
        hexadecimal = "".join(" ".join(lines[:-1]).split())
        try:
            device, inode, size = (int(field) for field in fields)
            data = bytes.fromhex(hexadecimal)
        except ValueError as error:
            raise RemoteFileAnswerError(self._watch.where) from error
        self._watch.observe((device, inode), size)
        return data, size


__all__ = ["MAX_READ_BYTES", "RemoteFileAnswerError", "SlurmFileSource"]
