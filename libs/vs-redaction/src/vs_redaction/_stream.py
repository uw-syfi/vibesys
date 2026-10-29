"""Incremental implementation of credential-safe diagnostic streams."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum, auto

from vs_redaction._patterns import SECRET_NAME

_MAX_PENDING_CHARACTERS = 1024 * 1024
_OVERSIZED_CANDIDATE = "[vibesys-redaction] oversized diagnostic token omitted"
_REDACTED = "[REDACTED]"
_SECRET_AT_END = re.compile(rf"\b(?P<name>{SECRET_NAME})\b$")
_BEARER_AT_END = re.compile(r"(?i)\b(?P<name>bearer)$")


class _Mode(Enum):
    TEXT = auto()
    ASSIGNMENT_SEPARATOR = auto()
    ASSIGNMENT_VALUE = auto()
    BEARER_VALUE = auto()
    SECRET_VALUE = auto()
    SEALED = auto()


def _is_token_character(character: str) -> bool:
    return character.isascii() and (character.isalnum() or character in "_-")


@dataclass(slots=True)
class DiagnosticRedactor:
    """Redact one complete diagnostic stream incrementally.

    ``append`` returns only text that is already known not to be a credential
    value. Candidate names and separators remain private until the next input
    proves whether they introduce a secret. ``finish`` resolves the final
    candidate and is idempotent. A pathological token cannot grow memory
    without bound: once the private candidate reaches the limit, the redactor
    emits a safe marker and conservatively suppresses the rest of the stream.
    """

    _mode: _Mode = _Mode.TEXT
    _pending: list[str] = field(default_factory=list)
    _pending_size: int = 0
    _finished: bool = False

    def append(self, text: str) -> str:
        """Consume the next stream fragment and return its credential-safe text."""
        if self._finished:
            message = "cannot append to a finished diagnostic stream"
            raise ValueError(message)
        emitted: list[str] = []
        for character in text:
            self._consume(character, emitted)
        return "".join(emitted)

    def finish(self) -> str:
        """Return the safe unresolved suffix; a second call returns no text."""
        if self._finished:
            return ""
        self._finished = True
        if self._mode not in {_Mode.SECRET_VALUE, _Mode.SEALED}:
            suffix = "".join(self._pending)
            self._clear_pending()
            return suffix
        self._clear_pending()
        return ""

    def _consume(self, character: str, emitted: list[str]) -> None:
        if self._mode is _Mode.SEALED:
            return
        if self._mode is _Mode.SECRET_VALUE:
            self._consume_secret_value(character, emitted)
            return
        if self._mode is _Mode.ASSIGNMENT_SEPARATOR:
            self._consume_assignment_separator(character, emitted)
            return
        if self._mode in {_Mode.ASSIGNMENT_VALUE, _Mode.BEARER_VALUE}:
            self._consume_value_prefix(character, emitted)
            return
        if _is_token_character(character):
            self._hold(character, emitted)
            return
        self._finish_token(character, emitted)

    def _consume_secret_value(self, character: str, emitted: list[str]) -> None:
        if character.isspace() or character in ",;":
            emitted.append(character)
            self._mode = _Mode.TEXT

    def _consume_assignment_separator(self, character: str, emitted: list[str]) -> None:
        if character.isspace():
            self._hold(character, emitted)
        elif character in ":=":
            self._hold(character, emitted)
            self._mode = _Mode.ASSIGNMENT_VALUE
        else:
            self._emit_pending(emitted)
            self._mode = _Mode.TEXT
            self._consume(character, emitted)

    def _consume_value_prefix(self, character: str, emitted: list[str]) -> None:
        if character.isspace():
            self._hold(character, emitted)
        elif character in ",;":
            self._emit_pending(emitted)
            emitted.append(character)
            self._mode = _Mode.TEXT
        else:
            self._emit_pending(emitted)
            emitted.append(_REDACTED)
            self._mode = _Mode.SECRET_VALUE

    def _finish_token(self, separator: str, emitted: list[str]) -> None:
        token = "".join(self._pending)
        if secret := _SECRET_AT_END.search(token):
            emitted.append(token[: secret.start("name")])
            name = secret.group("name")
            self._replace_pending(name)
            if separator.isspace():
                self._hold(separator, emitted)
                self._mode = _Mode.ASSIGNMENT_SEPARATOR
            elif separator in ":=":
                self._hold(separator, emitted)
                self._mode = _Mode.ASSIGNMENT_VALUE
            else:
                self._emit_pending(emitted)
                emitted.append(separator)
            return
        if (bearer := _BEARER_AT_END.search(token)) and separator.isspace():
            emitted.append(token[: bearer.start("name")])
            self._replace_pending(bearer.group("name"))
            self._hold(separator, emitted)
            self._mode = _Mode.BEARER_VALUE
            return
        self._emit_pending(emitted)
        emitted.append(separator)

    def _hold(self, text: str, emitted: list[str]) -> None:
        self._pending.append(text)
        self._pending_size += len(text)
        if self._pending_size <= _MAX_PENDING_CHARACTERS:
            return
        self._clear_pending()
        emitted.append(_OVERSIZED_CANDIDATE)
        self._mode = _Mode.SEALED

    def _replace_pending(self, text: str) -> None:
        self._pending[:] = [text]
        self._pending_size = len(text)

    def _emit_pending(self, emitted: list[str]) -> None:
        emitted.extend(self._pending)
        self._clear_pending()

    def _clear_pending(self) -> None:
        self._pending.clear()
        self._pending_size = 0


__all__ = ["DiagnosticRedactor"]
