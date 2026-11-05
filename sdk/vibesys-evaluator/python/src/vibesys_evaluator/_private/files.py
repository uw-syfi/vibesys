"""The schema's files: length-delimited ``loadgen.proto`` streams and single ``phase.proto`` messages.

A ``loadgen.proto`` file is length-delimited binary (``binpb``, what the
package writes and loadgen writes by default) or proto3 JSON, one message per
line (``json``, loadgen's ``--format json`` for debugging); both read alike.

Every file read is validated (protovalidate) here, at the boundary, so the
rest of the package works on messages that hold the schema's rules; errors
name the file and the field.
"""

from __future__ import annotations

import gzip
import importlib.metadata
import mmap
from typing import IO, TYPE_CHECKING, Literal, TypeVar

import protovalidate

from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb
from vibesys_evaluator._proto.vibesys.evaluator.v1 import phase_pb

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from protobuf import Message

SCHEMA_VERSION = 1
WRITER = f"vibesys-evaluator {importlib.metadata.version('vibesys-evaluator')}"
_T = TypeVar("_T", bound="Message")
Format = Literal["binpb", "json"]
_CONTINUE = 0x80  # LEB128: a byte below it ends a varint
_MAX_SHIFT = 63


class SchemaFileError(ValueError):
    """A file that does not hold what the schema says (the message names the file)."""


def _varint(value: int) -> bytes:
    out = bytearray()
    while value >= _CONTINUE:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _check(message: _T, where: str) -> _T:
    try:
        protovalidate.validate(message)
    except protovalidate.ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(e.field_name for e in v.proto.field.elements) if v.proto.field else '(message)'}: "
            f"{v.proto.message}"
            for v in error.violations[:5]
        )
        msg = f"{where}: invalid {type(message).__name__}: {problems}"
        raise SchemaFileError(msg) from error
    return message


class DelimitedWriter:
    """Writes length-delimited messages to a binary file."""

    def __init__(self, file: IO[bytes]) -> None:
        """Write to ``file`` (opened for binary writing)."""
        self._file = file

    def write(self, message: Message) -> None:
        """Append one message (encoding checks its types; validation is the reader's)."""
        data = message.to_binary()
        self._file.write(_varint(len(data)))
        self._file.write(data)

    def write_header(self, kind: pb.FileKind) -> None:
        """Write the file's ``FileHeader``."""
        self.write(pb.FileHeader(schema_version=SCHEMA_VERSION, kind=kind, writer=WRITER))


def _map(path: Path) -> bytes | mmap.mmap:
    """The file's bytes, mapped read-only; an empty file reads as no bytes.

    A large results file then stays in the page cache, out of the process's own memory.
    """
    with path.open("rb") as file:
        if path.stat().st_size == 0:
            return b""
        return mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ)


def _frames(data: bytes | mmap.mmap, where: str) -> Iterator[memoryview]:
    view = memoryview(data)
    position = 0
    while position < len(view):
        length = 0
        shift = 0
        while True:
            if position >= len(view) or shift > _MAX_SHIFT:
                msg = f"{where}: truncated length prefix at byte {position}"
                raise SchemaFileError(msg)
            byte = view[position]
            position += 1
            length |= (byte & 0x7F) << shift
            shift += 7
            if byte < _CONTINUE:
                break
        if position + length > len(view):
            msg = f"{where}: message of {length} bytes runs past the end of the file"
            raise SchemaFileError(msg)
        yield view[position : position + length]
        position += length


class DelimitedReader:
    """Reads a length-delimited file: its header first, then messages of one type."""

    def __init__(self, path: Path, kind: pb.FileKind, *, file_format: Format = "binpb") -> None:
        """Read ``path`` whole and check that its header says ``kind`` and schema version 1.

        Raises:
            SchemaFileError: The file is missing, empty or of another kind or version.
        """
        try:
            data = _map(path)
        except OSError as error:
            msg = f"{path}: cannot read ({error.strerror})"
            raise SchemaFileError(msg) from error
        self._format = file_format
        self._open(data, str(path), kind)

    @classmethod
    def from_bytes(cls, data: bytes, where: str, kind: pb.FileKind) -> DelimitedReader:
        """Read a file's contents already in memory; ``where`` names it in errors."""
        reader = cls.__new__(cls)
        reader._format = "binpb"
        reader._open(data, where, kind)
        return reader

    def _open(self, data: bytes | mmap.mmap, where: str, kind: pb.FileKind) -> None:
        path = where
        self._where = where
        self._frames: Iterator[memoryview | str] = (
            _frames(data, self._where) if self._format == "binpb" else _lines(data, self._where)
        )
        header = self._next(pb.FileHeader)
        if header is None:
            msg = f"{path}: empty file, expected a {kind.name.lower()} file"
            raise SchemaFileError(msg)
        if header.schema_version != SCHEMA_VERSION:
            msg = (
                f"{path}: unsupported schema version {header.schema_version} "
                f"(this reader implements {SCHEMA_VERSION})"
            )
            raise SchemaFileError(msg)
        if header.kind != kind:
            msg = f"{path}: FileHeader.kind is {header.kind.name}, expected {kind.name}"
            raise SchemaFileError(msg)
        self.header = header

    def _next(self, message_type: type[_T]) -> _T | None:
        frame = next(self._frames, None)
        if frame is None:
            return None
        try:
            message = (
                message_type.from_json(frame)
                if isinstance(frame, str)
                else message_type.from_binary(frame)
            )
        except (ValueError, TypeError) as error:
            msg = f"{self._where}: cannot decode a {message_type.__name__} ({error})"
            raise SchemaFileError(msg) from error
        return _check(message, self._where)

    def one(self, message_type: type[_T]) -> _T:
        """The next message, which must exist."""
        message = self._next(message_type)
        if message is None:
            msg = f"{self._where}: ends before its {message_type.__name__}"
            raise SchemaFileError(msg)
        return message

    def rest(self, message_type: type[_T]) -> Iterator[_T]:
        """The remaining messages."""
        while (message := self._next(message_type)) is not None:
            yield message


def _lines(data: bytes | mmap.mmap, where: str) -> Iterator[str]:
    try:
        text = bytes(data).decode()
    except UnicodeDecodeError as error:
        msg = f"{where}: not UTF-8 JSON lines ({error})"
        raise SchemaFileError(msg) from error
    yield from (line for line in text.splitlines() if line.strip())


def _captured(operation: pb.PlannedOperation, where: str) -> None:
    """Every ref of a step names a capture of an earlier step of the operation."""
    captured: set[str] = set()
    for step in operation.steps:
        for name in _refs(step):
            if name not in captured:
                msg = f"{where}: ref {name!r} names no capture of an earlier step"
                raise SchemaFileError(msg)
        captured.update(capture.name for capture in step.captures)


def _refs(step: pb.Step) -> Iterator[str]:
    call = step.call.value if step.call is not None else None
    if not isinstance(call, pb.HttpCall):
        return
    templates = [call.path, *(p.value for p in call.query), *(p.value for p in call.headers)]
    if call.body is not None:
        body = call.body.value
        if isinstance(body, pb.Template):
            templates.append(body)
        elif isinstance(body, pb.Form):
            templates.extend(f.value for f in body.fields)
    for template in templates:
        for part in template.parts if template is not None else ():
            if part.part is not None and isinstance(part.part.value, pb.Ref):
                yield part.part.value.name


def read_requests(
    path: Path, *, file_format: Format = "binpb"
) -> tuple[pb.RequestsHeader, list[pb.PlannedOperation]]:
    """A requests file, with the rules loadgen holds a plan to (count, refs)."""
    reader = DelimitedReader(path, pb.FileKind.REQUESTS, file_format=file_format)
    header = reader.one(pb.RequestsHeader)
    operations = list(reader.rest(pb.PlannedOperation))
    if header.count != len(operations):
        msg = (
            f"{path}: RequestsHeader.count is {header.count} "
            f"but the file holds {len(operations)} operations"
        )
        raise SchemaFileError(msg)
    for index, operation in enumerate(operations):
        _captured(operation, f"{path}: operation {index}")
    return header, operations


def read_summary(path: Path) -> pb.PhaseSummary:
    """The ``PhaseSummary`` of a summary file."""
    return DelimitedReader(path, pb.FileKind.SUMMARY).one(pb.PhaseSummary)


def read_results(path: Path) -> list[pb.OperationResult]:
    """Every ``OperationResult`` of a results file, in completion order."""
    return list(iter_results(path))


def iter_results(path: Path) -> Iterator[pb.OperationResult]:
    """The ``OperationResult``s of a results file one at a time, in completion order.

    A reader that keeps few of them holds few in memory.
    """
    return DelimitedReader(path, pb.FileKind.RESULTS).rest(pb.OperationResult)


def read_results_gz(path: Path) -> list[pb.OperationResult]:
    """Every ``OperationResult`` of a gzip-compressed results file (a phase's artifact)."""
    try:
        data = gzip.decompress(path.read_bytes())
    except (OSError, EOFError) as error:
        msg = f"{path}: cannot read ({error})"
        raise SchemaFileError(msg) from error
    reader = DelimitedReader.from_bytes(data, str(path), pb.FileKind.RESULTS)
    return list(reader.rest(pb.OperationResult))


def write_message(path: Path, message: Message) -> None:
    """Write one message as a whole binpb file (``phase.proto`` files)."""
    _check(message, str(path))
    path.write_bytes(message.to_binary())


def read_message(path: Path, message_type: type[_T]) -> _T:
    """Read and validate a whole-file binpb message."""
    try:
        data = path.read_bytes()
    except OSError as error:
        msg = f"{path}: cannot read ({error.strerror})"
        raise SchemaFileError(msg) from error
    try:
        message = message_type.from_binary(data)
    except (ValueError, TypeError) as error:
        msg = f"{path}: cannot decode a {message_type.__name__} ({error})"
        raise SchemaFileError(msg) from error
    return _check(message, str(path))


def phase_input(data: bytes, where: str) -> phase_pb.PhaseInput:
    """Decode and validate a ``PhaseInput``."""
    try:
        message = phase_pb.PhaseInput.from_binary(data)
    except (ValueError, TypeError) as error:
        msg = f"{where}: cannot decode a PhaseInput ({error})"
        raise SchemaFileError(msg) from error
    return _check(message, where)


def write_delimited(path: Path, kind: pb.FileKind, messages: Sequence[Message]) -> None:
    """Write a whole length-delimited file (tests and tools)."""
    with path.open("wb") as file:
        writer = DelimitedWriter(file)
        writer.write_header(kind)
        for message in messages:
            writer.write(message)
