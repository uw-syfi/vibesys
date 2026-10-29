"""Decode JSON from outside the process into a typed model, or say why it cannot.

This is the one decode path for bytes a store or an edge adapter receives: a file
another process or an agent wrote, a CLI's output, a peer's frame. Each step bounds
what hostile input can cost:

1. The input is capped at ``max_bytes`` (a file is read at most one byte past it).
2. The native JSON parser preserves object members for a duplicate-key pass.
3. pydantic's JSON parser parses it; it bounds nesting depth, so deep input is a
   validation error, not a ``RecursionError``, and bad UTF-8 is an error too.
4. The parsed value is validated against a strict target: every model, dataclass and
   TypedDict the target decodes, at any depth, must forbid unknown keys.

Every failure is returned as an ``Unreadable`` value naming the source, the kind of
failure and the key path, never echoing the input. Nothing here raises for bad input.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from io import StringIO
from typing import TYPE_CHECKING, Any, ClassVar, overload

from pydantic import BaseModel, TypeAdapter, ValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class ValidationOptions:
    """How pydantic validates the parsed value."""

    strict: bool | None = None
    """pydantic's strict mode: no coercion between JSON types.

    ``None`` keeps the target's own ``strict`` setting; ``False`` would switch a strict
    model to lax validation.
    """
    context: Mapping[str, Any] | None = None
    """Validation context handed to the target's validators."""


DEFAULT_OPTIONS = ValidationOptions()


class UnreadableKind(StrEnum):
    """Why input could not become the target type."""

    MISSING = "missing"
    NOT_READABLE = "not_readable"
    TOO_LARGE = "too_large"
    NOT_JSON = "not_json"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class Problem:
    """One validation failure: where in the document, and what is wrong there."""

    location: tuple[str | int, ...]
    """Keys and list indexes from the document root to the failing value; empty for the
    document itself."""
    message: str
    error_type: str | None = None
    """pydantic's error type (``"missing"``, ``"extra_forbidden"``, or a validator's own),
    for a caller that maps failures onto its own reason codes; ``None`` for a problem found
    before validation (a missing, unreadable or oversized input)."""

    @property
    def key_path(self) -> str:
        """``location`` as a dotted path; empty for the document itself."""
        return ".".join(str(part) for part in self.location)


class _ObjectState(StrEnum):
    KEY_OR_END = "key_or_end"
    KEY = "key"
    COLON = "colon"
    VALUE = "value"
    COMMA_OR_END = "comma_or_end"


class _ArrayState(StrEnum):
    VALUE_OR_END = "value_or_end"
    VALUE = "value"
    COMMA_OR_END = "comma_or_end"


@dataclass(frozen=True, slots=True)
class _PathNode:
    parent: _PathNode | None
    part: str | int


type _JsonPath = _PathNode | None


def _path_location(path: _PathNode) -> tuple[str | int, ...]:
    parts: list[str | int] = []
    current: _PathNode | None = path
    while current is not None:
        parts.append(current.part)
        current = current.parent
    parts.reverse()
    return tuple(parts)


@dataclass(slots=True)
class _ObjectFrame:
    path: _JsonPath
    keys: set[str]
    state: _ObjectState = _ObjectState.KEY_OR_END
    key: str | None = None


@dataclass(slots=True)
class _ArrayFrame:
    path: _JsonPath
    state: _ArrayState = _ArrayState.VALUE_OR_END
    next_index: int = 0


type _ContainerFrame = _ObjectFrame | _ArrayFrame

_MAX_JSON_NESTING = 200
"""pydantic-core's accepted JSON container depth, pinned by boundary tests below."""
_JSON_CONTROL_CHARACTER_LIMIT = 0x20
_UTF16_HIGH_SURROGATE_START = 0xD800
_UTF16_HIGH_SURROGATE_END = 0xDBFF
_UTF16_LOW_SURROGATE_START = 0xDC00
_UTF16_LOW_SURROGATE_END = 0xDFFF
_UNICODE_ESCAPE_DIGITS = 4
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_STRING_SPECIAL = re.compile(r'["\\\x00-\x1f\ud800-\udfff]')


def _invalid_unescaped_string_character(character: str) -> bool:
    code_point = ord(character)
    return code_point < _JSON_CONTROL_CHARACTER_LIMIT or (
        _UTF16_HIGH_SURROGATE_START <= code_point <= _UTF16_LOW_SURROGATE_END
    )


class _StringChunks:
    """Decode escaped string chunks without one container entry per character."""

    def __init__(self, *, retain: bool, max_characters: int | None) -> None:
        self._output = StringIO() if retain else None
        self._remaining = max_characters

    @property
    def retaining(self) -> bool:
        return self._output is not None

    def append_slice(self, text: str, start: int, end: int) -> None:
        output = self._reserve(end - start)
        if output is not None:
            output.write(text[start:end])

    def append(self, value: str) -> None:
        output = self._reserve(len(value))
        if output is not None:
            output.write(value)

    def value(self) -> str | None:
        return None if self._output is None else self._output.getvalue()

    def _reserve(self, characters: int) -> StringIO | None:
        if self._output is None:
            return None
        if self._remaining is not None:
            if characters > self._remaining:
                self._output = None
                return None
            self._remaining -= characters
        return self._output


class _DuplicateKeyScanner:
    """Find a repeated object member without recursively parsing untrusted JSON."""

    _ESCAPES: ClassVar[dict[str, str]] = {
        '"': '"',
        "\\": "\\",
        "/": "/",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
    }

    def __init__(
        self,
        data: bytes | str,
        *,
        capture: frozenset[str] = frozenset(),
        capture_limit: int | None = None,
    ) -> None:
        try:
            self._text = data.decode("utf-8") if isinstance(data, bytes) else data
        except UnicodeDecodeError:
            self._text = ""
            self._decodable = False
        else:
            self._decodable = True
        self._position = 0
        self._stack: list[_ContainerFrame] = []
        self._duplicate: Problem | None = None
        self._capture = capture
        self._capture_limit = capture_limit
        self._captured: dict[str, str] = {}
        self._ambiguous: set[str] = set()
        self._complete = False

    def find(self) -> Problem | None:
        """Return the first duplicate key only when the surrounding JSON syntax is complete."""
        if not self._decodable:
            return None
        root_started = False
        while True:
            self._skip_whitespace()
            if self._stack:
                frame = self._stack[-1]
                valid = (
                    self._step_object(frame)
                    if isinstance(frame, _ObjectFrame)
                    else self._step_array(frame)
                )
                if not valid:
                    return None
                continue
            if root_started:
                if self._position != len(self._text):
                    return None
                self._complete = True
                return self._duplicate
            root_started = True
            if not self._start_value(None, None):
                return None

    @property
    def captured(self) -> dict[str, str]:
        """Requested root-object strings seen exactly once in complete JSON."""
        return self._captured.copy() if self._complete else {}

    def _step_object(self, frame: _ObjectFrame) -> bool:
        match frame.state:
            case _ObjectState.KEY_OR_END:
                return self._step_object_key(frame, allow_end=True)
            case _ObjectState.KEY:
                return self._step_object_key(frame, allow_end=False)
            case _ObjectState.COLON:
                return self._step_object_colon(frame)
            case _ObjectState.VALUE:
                return self._step_object_value(frame)
            case _ObjectState.COMMA_OR_END:
                return self._step_object_delimiter(frame)

    def _step_object_key(self, frame: _ObjectFrame, *, allow_end: bool) -> bool:
        if allow_end and self._current() == "}":
            self._position += 1
            self._stack.pop()
            return True
        scanned = self._scan_string(decode=True)
        if scanned is None:
            return False
        key, self._position = scanned
        if key is None:
            return False
        if key in frame.keys and self._duplicate is None:
            self._duplicate = Problem(
                _path_location(_PathNode(frame.path, key)),
                "Object member appears more than once",
                "duplicate_key",
            )
        if key in frame.keys and frame.path is None and key in self._capture:
            self._ambiguous.add(key)
            self._captured.pop(key, None)
        frame.keys.add(key)
        frame.key = key
        frame.state = _ObjectState.COLON
        return True

    def _step_object_colon(self, frame: _ObjectFrame) -> bool:
        if self._current() != ":":
            return False
        self._position += 1
        frame.state = _ObjectState.VALUE
        return True

    def _step_object_value(self, frame: _ObjectFrame) -> bool:
        self._skip_whitespace()
        key = frame.key
        if key is None:
            return False
        frame.key = None
        frame.state = _ObjectState.COMMA_OR_END
        return self._start_value(frame.path, key)

    def _step_object_delimiter(self, frame: _ObjectFrame) -> bool:
        if self._current() == ",":
            self._position += 1
            frame.state = _ObjectState.KEY
            return True
        if self._current() != "}":
            return False
        self._position += 1
        self._stack.pop()
        return True

    def _step_array(self, frame: _ArrayFrame) -> bool:
        state = frame.state
        character = self._current()
        if state in {_ArrayState.VALUE_OR_END, _ArrayState.VALUE}:
            if state is _ArrayState.VALUE_OR_END and character == "]":
                self._position += 1
                self._stack.pop()
                return True
            index = frame.next_index
            frame.next_index += 1
            frame.state = _ArrayState.COMMA_OR_END
            return self._start_value(frame.path, index)
        if character == ",":
            self._position += 1
            frame.state = _ArrayState.VALUE
            return True
        if character == "]":
            self._position += 1
            self._stack.pop()
            return True
        return False

    def _start_value(self, parent_path: _JsonPath, part: str | int | None) -> bool:
        self._skip_whitespace()
        character = self._current()
        if character == "{":
            if len(self._stack) >= _MAX_JSON_NESTING:
                return False
            self._position += 1
            path = parent_path if part is None else _PathNode(parent_path, part)
            self._stack.append(_ObjectFrame(path, set()))
        elif character == "[":
            if len(self._stack) >= _MAX_JSON_NESTING:
                return False
            self._position += 1
            path = parent_path if part is None else _PathNode(parent_path, part)
            self._stack.append(_ArrayFrame(path))
        elif character == '"':
            capture = (
                isinstance(part, str)
                and parent_path is None
                and part in self._capture
                and part not in self._ambiguous
            )
            scanned = self._scan_string(
                decode=capture,
                max_decoded_characters=(self._capture_limit if capture else None),
            )
            if scanned is None:
                return False
            value, self._position = scanned
            if capture and value is not None:
                self._captured[part] = value
        else:
            literal = next(
                (
                    candidate
                    for candidate in ("true", "false", "null", "NaN", "Infinity", "-Infinity")
                    if self._text.startswith(candidate, self._position)
                ),
                None,
            )
            if literal is not None:
                self._position += len(literal)
            else:
                number_end = self._scan_number()
                if number_end is None:
                    return False
                self._position = number_end
        return True

    def _scan_string(
        self, *, decode: bool, max_decoded_characters: int | None = None
    ) -> tuple[str | None, int] | None:
        if self._current() != '"':
            return None
        start = self._position + 1
        match = _STRING_SPECIAL.search(self._text, start)
        if match is None or _invalid_unescaped_string_character(match.group()):
            return None
        if match.group() == '"':
            return self._unescaped_string(
                start, match, decode=decode, max_characters=max_decoded_characters
            )
        return self._scan_escaped_string(
            start,
            match,
            decode=decode,
            max_decoded_characters=max_decoded_characters,
        )

    def _unescaped_string(
        self,
        start: int,
        closing: re.Match[str],
        *,
        decode: bool,
        max_characters: int | None,
    ) -> tuple[str | None, int]:
        if not decode or (max_characters is not None and closing.start() - start > max_characters):
            return None, closing.end()
        return self._text[start : closing.start()], closing.end()

    def _scan_escaped_string(
        self,
        start: int,
        escape: re.Match[str],
        *,
        decode: bool,
        max_decoded_characters: int | None,
    ) -> tuple[str | None, int] | None:
        chunks = _StringChunks(retain=decode, max_characters=max_decoded_characters)
        position = start
        while True:
            chunks.append_slice(self._text, position, escape.start())
            escaped = self._scan_escape(escape.end(), decode=chunks.retaining)
            if escaped is None:
                return None
            value, position = escaped
            if value is not None:
                chunks.append(value)
            following = _STRING_SPECIAL.search(self._text, position)
            if following is None or _invalid_unescaped_string_character(following.group()):
                return None
            if following.group() == '"':
                chunks.append_slice(self._text, position, following.start())
                return chunks.value(), following.end()
            escape = following

    def _scan_escape(self, position: int, *, decode: bool) -> tuple[str | None, int] | None:
        if position >= len(self._text):
            return None
        escape = self._text[position]
        replacement = self._ESCAPES.get(escape)
        if replacement is not None:
            return (replacement if decode else None), position + 1
        if escape != "u":
            return None
        unicode_escape = self._unicode_escape(position + 1)
        if unicode_escape is None:
            return None
        combined = self._combine_surrogate(*unicode_escape)
        if combined is None:
            return None
        code_point, end = combined
        return (chr(code_point) if decode else None), end

    def _combine_surrogate(self, code_point: int, end: int) -> tuple[int, int] | None:
        if _UTF16_LOW_SURROGATE_START <= code_point <= _UTF16_LOW_SURROGATE_END:
            return None
        if not (_UTF16_HIGH_SURROGATE_START <= code_point <= _UTF16_HIGH_SURROGATE_END):
            return code_point, end
        if not self._text.startswith("\\u", end):
            return None
        low_escape = self._unicode_escape(end + 2)
        if low_escape is None or not (
            _UTF16_LOW_SURROGATE_START <= low_escape[0] <= _UTF16_LOW_SURROGATE_END
        ):
            return None
        combined = (
            0x10000
            + ((code_point - _UTF16_HIGH_SURROGATE_START) << 10)
            + (low_escape[0] - _UTF16_LOW_SURROGATE_START)
        )
        return combined, low_escape[1]

    def _unicode_escape(self, position: int) -> tuple[int, int] | None:
        end = position + _UNICODE_ESCAPE_DIGITS
        digits = self._text[position:end]
        if len(digits) != _UNICODE_ESCAPE_DIGITS or any(
            character not in "0123456789abcdefABCDEF" for character in digits
        ):
            return None
        return int(digits, 16), end

    def _scan_number(self) -> int | None:
        matched = _NUMBER.match(self._text, self._position)
        return None if matched is None else matched.end()

    def _skip_whitespace(self) -> None:
        while self._position < len(self._text) and self._text[self._position] in " \t\r\n":
            self._position += 1

    def _current(self) -> str | None:
        return self._text[self._position] if self._position < len(self._text) else None


@dataclass(frozen=True, slots=True)
class _ObjectPairs:
    pairs: list[tuple[str, object]]


_JSON_SYNTAX = TypeAdapter(object)


def _duplicate_key_problem(data: bytes | str) -> Problem | None:
    # The lexical scanner also extracts correlation strings, but walking every
    # token in Python makes observing a large run record dominate admission.
    # Preserve all object members with the native parser, then visit keys in
    # document order. The target parser remains authoritative for JSON syntax.
    try:
        parsed = json.loads(data, object_pairs_hook=_ObjectPairs)
    except (ValueError, UnicodeError, RecursionError):
        return None
    duplicate = _first_duplicate(parsed)
    if duplicate is not None:
        try:
            _JSON_SYNTAX.validate_json(data)
        except ValidationError:
            return None
    return duplicate


def _first_duplicate(parsed: object) -> Problem | None:
    pending: list[tuple[object, _JsonPath]] = [(parsed, None)]
    while pending:
        value, path = pending.pop()
        if isinstance(value, Problem):
            return value
        children: list[tuple[object, _JsonPath]] = []
        if isinstance(value, _ObjectPairs):
            seen: set[str] = set()
            for key, member in value.pairs:
                child = _PathNode(path, key)
                if key in seen:
                    problem = Problem(
                        _path_location(child),
                        "Object member appears more than once",
                        "duplicate_key",
                    )
                    children.append((problem, child))
                    break
                seen.add(key)
                children.append((member, child))
        elif isinstance(value, list):
            children = [(member, _PathNode(path, index)) for index, member in enumerate(value)]
        pending.extend(reversed(children))
    return None


def _input_size(data: bytes | str) -> int:
    return len(data) if isinstance(data, bytes) else len(data.encode("utf-8", "surrogatepass"))


def extract_unique_object_strings(
    data: bytes | str,
    fields: frozenset[str],
    *,
    max_bytes: int,
    max_value_characters: int,
) -> dict[str, str]:
    """Return requested string members that occur once in a complete root JSON object.

    This is the narrow inspection path for correlation metadata on an otherwise rejected
    document. Invalid, oversized, non-object, non-string, and repeated requested members
    are absent. Requested values longer than ``max_value_characters`` are absent too.
    Values are decoded only for explicitly requested members; other string values are
    validated without being retained.
    """
    if max_value_characters < 0:
        message = "max_value_characters must be non-negative"
        raise ValueError(message)
    if _input_size(data) > max_bytes:
        return {}
    scanner = _DuplicateKeyScanner(data, capture=fields, capture_limit=max_value_characters)
    scanner.find()
    return scanner.captured


@dataclass(frozen=True, slots=True)
class Unreadable:
    """Input that is not a valid document of the target type, and why."""

    kind: UnreadableKind
    source: str
    """Where the input came from: a file path, a command, a peer."""
    problems: tuple[Problem, ...]

    @property
    def key_path(self) -> str:
        """The first failing key path, or empty when the document as a whole failed."""
        return self.problems[0].key_path if self.problems else ""

    def details(self, document: str = "<document>") -> str:
        """Every problem as ``path: message``, joined; ``document`` names the empty path."""
        return "; ".join(f"{p.key_path or document}: {p.message}" for p in self.problems)

    def __str__(self) -> str:
        """The source, the kind and the details, without the input."""
        return f"{self.source}: {self.kind.value}: {self.details()}"


def _lenient_classes(schema: object) -> list[str]:
    """Every model, dataclass and TypedDict a core schema decodes that keeps unknown keys.

    Root models decode no keys of their own. Pydantic's ``BaseModel`` itself declares no
    fields: a field typed with it is an opaque slot that its owner resolves in a validator
    (for example through a registry), not a document shape, so it is not counted.
    """
    found: list[str] = []
    if isinstance(schema, dict):
        decodes_keys = not schema.get("root_model") and schema.get("cls") is not BaseModel
        if decodes_keys and schema.get("type") in ("model", "dataclass", "typed-dict"):
            config = schema.get("config") or {}
            extra = schema.get("extra_behavior") or config.get("extra_fields_behavior")
            if extra != "forbid":
                found.append(schema["cls"].__qualname__)
        for value in schema.values():
            found.extend(_lenient_classes(value))
    elif isinstance(schema, list):
        for value in schema:
            found.extend(_lenient_classes(value))
    return found


@lru_cache(maxsize=256)
def _require_strict(target: type[BaseModel] | TypeAdapter[Any]) -> None:
    """Refuse a target that would drop an unknown key anywhere in the document.

    Checked before any input is read, so a lenient target fails whatever the input.
    """
    schema = (
        target.core_schema if isinstance(target, TypeAdapter) else target.__pydantic_core_schema__
    )
    lenient = sorted(set(_lenient_classes(schema)))
    if lenient:
        message = (
            f"{', '.join(lenient)} must set extra='forbid' to be decoded: "
            "an unknown key in untrusted input is an error, not something to drop"
        )
        raise TypeError(message)


def _unreadable(source: str, error: ValidationError) -> Unreadable:
    details = error.errors(include_url=False, include_context=False, include_input=False)
    problems = tuple(
        Problem(tuple(detail["loc"]), detail["msg"], detail["type"]) for detail in details
    )
    not_json = any(detail["type"] == "json_invalid" for detail in details)
    kind = UnreadableKind.NOT_JSON if not_json else UnreadableKind.INVALID
    return Unreadable(kind, source, problems)


def _too_large(source: str, max_bytes: int) -> Unreadable:
    return Unreadable(
        UnreadableKind.TOO_LARGE, source, (Problem((), f"is larger than {max_bytes} bytes"),)
    )


@overload
def decode_json[ModelT: BaseModel](
    data: bytes | str,
    target: type[ModelT],
    *,
    source: str,
    max_bytes: int,
    options: ValidationOptions = DEFAULT_OPTIONS,
) -> ModelT | Unreadable: ...


@overload
def decode_json[ValueT](
    data: bytes | str,
    target: TypeAdapter[ValueT],
    *,
    source: str,
    max_bytes: int,
    options: ValidationOptions = DEFAULT_OPTIONS,
) -> ValueT | Unreadable: ...


def decode_json(
    data: bytes | str,
    target: type[BaseModel] | TypeAdapter[Any],
    *,
    source: str,
    max_bytes: int,
    options: ValidationOptions = DEFAULT_OPTIONS,
) -> object:
    """Decode ``data`` as ``target``, or return why it is not one.

    ``target`` is a ``BaseModel`` subclass, or a ``TypeAdapter`` for values that are
    not models, such as a third-party tool's output. Repeated object keys are invalid
    at every depth. Every model, dataclass and
    TypedDict it decodes, at any depth, must forbid unknown keys (``TypeError``
    otherwise, whatever the input: that is a programming error). ``max_bytes`` caps the input size
    (UTF-8 bytes for ``str``); ``options`` selects strict mode and validation context.
    """
    _require_strict(target)
    size = _input_size(data)
    if size > max_bytes:
        return _too_large(source, max_bytes)
    if duplicate := _duplicate_key_problem(data):
        return Unreadable(UnreadableKind.INVALID, source, (duplicate,))
    try:
        if isinstance(target, TypeAdapter):
            return target.validate_json(data, strict=options.strict, context=options.context)
        return target.model_validate_json(data, strict=options.strict, context=options.context)
    except ValidationError as error:
        return _unreadable(source, error)


@overload
def read_json[ModelT: BaseModel](
    path: Path,
    target: type[ModelT],
    *,
    max_bytes: int,
    options: ValidationOptions = DEFAULT_OPTIONS,
    follow_symlinks: bool = True,
) -> ModelT | Unreadable: ...


@overload
def read_json[ValueT](
    path: Path,
    target: TypeAdapter[ValueT],
    *,
    max_bytes: int,
    options: ValidationOptions = DEFAULT_OPTIONS,
    follow_symlinks: bool = True,
) -> ValueT | Unreadable: ...


def read_json(
    path: Path,
    target: type[BaseModel] | TypeAdapter[Any],
    *,
    max_bytes: int,
    options: ValidationOptions = DEFAULT_OPTIONS,
    follow_symlinks: bool = True,
) -> object:
    """Read at most ``max_bytes`` of the regular file at ``path`` and decode it as ``target``.

    A missing file, one that cannot be read, one past the cap, and one that is not a
    valid document each come back as an ``Unreadable`` naming ``path``. Anything at
    ``path`` but a regular file (a directory, a FIFO, a device) is not readable: the file
    is opened without blocking and checked on the open descriptor, so a FIFO swapped in
    after a caller's own check cannot hang the reader. With ``follow_symlinks=False`` a
    symlink as the last component is not readable either.
    """
    _require_strict(target)
    source = str(path)
    data = _read_regular_file(path, max_bytes, follow_symlinks=follow_symlinks)
    if isinstance(data, Unreadable):
        return data
    if len(data) > max_bytes:
        return _too_large(source, max_bytes)
    if isinstance(target, TypeAdapter):
        return decode_json(data, target, source=source, max_bytes=max_bytes, options=options)
    return decode_json(data, target, source=source, max_bytes=max_bytes, options=options)


def _read_regular_file(path: Path, max_bytes: int, *, follow_symlinks: bool) -> bytes | Unreadable:
    """At most ``max_bytes + 1`` bytes of the regular file at ``path``, or why there are none."""
    source = str(path)
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | (0 if follow_symlinks else os.O_NOFOLLOW)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return Unreadable(UnreadableKind.MISSING, source, (Problem((), "does not exist"),))
    except OSError as error:
        return _not_readable(source, error)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return Unreadable(
                UnreadableKind.NOT_READABLE, source, (Problem((), "is not a regular file"),)
            )
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining and (chunk := os.read(descriptor, remaining)):
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    except OSError as error:
        return _not_readable(source, error)
    finally:
        os.close(descriptor)


def _not_readable(source: str, error: OSError) -> Unreadable:
    reason = error.strerror or type(error).__name__
    return Unreadable(UnreadableKind.NOT_READABLE, source, (Problem((), reason),))


__all__ = [
    "DEFAULT_OPTIONS",
    "Problem",
    "Unreadable",
    "UnreadableKind",
    "ValidationOptions",
    "decode_json",
    "extract_unique_object_strings",
    "read_json",
]
