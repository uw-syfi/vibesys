"""A Python interpreter for the shell commands the Fake cluster's remote filesystem receives.

The executing Fake cluster's "remote" is the local filesystem, so every ``exec`` request
used to start ``bash -c``. Most of those requests are the small, fixed vocabulary that
``remote_operations`` and ``staging`` build: ``mkdir``, ``mv``, ``ln``, ``rm``, ``cat``,
``printf``, ``test``/``[`` and ``sync`` joined by ``;``, ``&&`` and ``if ... fi``. Each one
cost a process start (tens of milliseconds, hundreds per scenario). :func:`run` executes
that vocabulary in-process and returns ``None`` for anything else, which the caller runs
under ``bash`` as before. Parsing finishes before anything runs, so a command is either
interpreted completely or not at all.

The interpreter is a Fake of those programs: ``tests/test_fake_shell.py`` checks it against
``bash`` over the commands production builds. Two behaviors are deliberately not modeled:
``sync`` does nothing (durability across a crash is not observable on a live filesystem),
and the text of error messages on stderr is approximate (the exit status is exact).
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_PUNCTUATION = ";&|<>()"
_UNARY_TEST_WORDS = 2  # an operator and a path
_SOURCE_AND_DESTINATION = 2
_SEQUENCE_END = ";"
_CONJUNCTION = "&&"
_DISCARD_STDERR = ["2", ">", "/dev/null"]
_TEST_OPERATORS = {
    "-f": lambda path: path.is_file(),
    "-e": lambda path: path.exists(),
    "-d": lambda path: path.is_dir(),
    "-L": lambda path: path.is_symlink(),
}


class _UnsupportedError(Exception):
    """The command uses shell syntax or programs outside the interpreted vocabulary."""


@dataclass
class _Streams:
    stdout: list[str] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)


def run(command: str) -> tuple[int, str, str] | None:
    """Run ``command`` and return ``(status, stdout, stderr)``, or ``None`` if unsupported."""
    try:
        action = _Parser(_words(command)).program()
    except _UnsupportedError:
        return None
    streams = _Streams()
    status = action(streams)
    return status, "".join(streams.stdout), "".join(streams.stderr)


def _words(command: str) -> list[str]:
    if "\n" in command or "$" in command or "`" in command:
        raise _UnsupportedError
    lexer = shlex.shlex(command, posix=True, punctuation_chars=_PUNCTUATION)
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError as error:  # unbalanced quoting
        raise _UnsupportedError from error
    if any(any(ch in word for ch in "*?") for word in words):
        raise _UnsupportedError  # globbing is not interpreted
    return words


class _Parser:
    """Recursive descent over ``list := and_or (';' and_or)*`` with ``if`` commands."""

    def __init__(self, words: list[str]) -> None:
        self._words = words
        self._at = 0

    def program(self) -> Callable[[_Streams], int]:
        action = self._sequence(frozenset())
        if self._peek() is not None:
            raise _UnsupportedError
        return action

    def _peek(self) -> str | None:
        return self._words[self._at] if self._at < len(self._words) else None

    def _take(self) -> str:
        word = self._peek()
        if word is None:
            raise _UnsupportedError
        self._at += 1
        return word

    def _expect(self, word: str) -> None:
        if self._take() != word:
            raise _UnsupportedError

    def _sequence(self, stops: frozenset[str]) -> Callable[[_Streams], int]:
        steps = [self._conjunction()]
        while self._peek() == _SEQUENCE_END:
            self._take()
            if self._peek() is None or self._peek() in stops:
                break
            steps.append(self._conjunction())

        def sequence(streams: _Streams) -> int:
            status = 0
            for step in steps:
                status = step(streams)
            return status

        return sequence

    def _conjunction(self) -> Callable[[_Streams], int]:
        steps = [self._command()]
        while self._peek() == _CONJUNCTION:
            self._take()
            steps.append(self._command())

        def conjunction(streams: _Streams) -> int:
            status = 0
            for step in steps:
                status = step(streams)
                if status != 0:
                    break
            return status

        return conjunction

    def _command(self) -> Callable[[_Streams], int]:
        if self._peek() == "if":
            return self._if()
        return self._simple()

    def _if(self) -> Callable[[_Streams], int]:
        self._expect("if")
        branches = []
        while True:
            condition = self._sequence(frozenset({"then"}))
            self._expect("then")
            body = self._sequence(frozenset({"elif", "else", "fi"}))
            branches.append((condition, body))
            if self._peek() != "elif":
                break
            self._take()
        otherwise = None
        if self._peek() == "else":
            self._take()
            otherwise = self._sequence(frozenset({"fi"}))
        self._expect("fi")

        def conditional(streams: _Streams) -> int:
            for condition, body in branches:
                if condition(streams) == 0:
                    return body(streams)
            return otherwise(streams) if otherwise is not None else 0

        return conditional

    def _simple(self) -> Callable[[_Streams], int]:
        words: list[str] = []
        while (word := self._peek()) is not None and word not in {_SEQUENCE_END, _CONJUNCTION}:
            words.append(self._take())
        quiet = words[-3:] == _DISCARD_STDERR
        if quiet:
            words = words[:-3]
        if not words or any(set(word) <= set(_PUNCTUATION) for word in words):
            raise _UnsupportedError
        program = _program(words[0], words[1:])

        def simple(streams: _Streams) -> int:
            errors = _Streams() if quiet else streams
            return program(streams.stdout, errors.stderr)

        return simple


def _program(name: str, args: list[str]) -> Callable[[list[str], list[str]], int]:
    """Bind one simple command, rejecting unknown programs and options at parse time."""
    builders = {
        "mkdir": _mkdir,
        "printf": _printf,
        "cat": _cat,
        "test": _test,
        "[": _bracket,
        "mv": _mv,
        "ln": _ln,
        "rm": _rm,
        "sync": _sync,
    }
    if name not in builders:
        raise _UnsupportedError
    return builders[name](args)


def _fail(errors: list[str], program: str, subject: str, error: OSError) -> int:
    errors.append(f"{program}: {subject}: {error.strerror}\n")
    return 1


def _paths(args: list[str], *, flags: frozenset[str]) -> tuple[frozenset[str], list[Path]]:
    given: set[str] = set()
    rest = list(args)
    while rest and rest[0].startswith("-"):
        flag = rest.pop(0)
        if flag == "--":
            break
        if flag not in flags:
            raise _UnsupportedError
        given.add(flag)
    return frozenset(given), [Path(item) for item in rest]


def _mkdir(args: list[str]) -> Callable[[list[str], list[str]], int]:
    given, paths = _paths(args, flags=frozenset({"-p"}))
    if not paths:
        raise _UnsupportedError

    def mkdir(_out: list[str], errors: list[str]) -> int:
        status = 0
        for path in paths:
            try:
                path.mkdir(parents="-p" in given, exist_ok="-p" in given)
            except OSError as error:
                status = _fail(errors, "mkdir", str(path), error)
        return status

    return mkdir


def _printf(args: list[str]) -> Callable[[list[str], list[str]], int]:
    if not args or "\\" in args[0]:
        raise _UnsupportedError
    template, values = args[0], args[1:]
    if template == "%s":
        text = "".join(values)
    elif "%" not in template and not values:
        text = template
    else:
        raise _UnsupportedError

    def printf(out: list[str], _errors: list[str]) -> int:
        out.append(text)
        return 0

    return printf


def _cat(args: list[str]) -> Callable[[list[str], list[str]], int]:
    _, paths = _paths(args, flags=frozenset())
    if not paths:
        raise _UnsupportedError

    def cat(out: list[str], errors: list[str]) -> int:
        status = 0
        for path in paths:
            try:
                out.append(path.read_bytes().decode("utf-8", errors="replace"))
            except OSError as error:
                status = _fail(errors, "cat", str(path), error)
        return status

    return cat


def _test(args: list[str]) -> Callable[[list[str], list[str]], int]:
    negate = args[:1] == ["!"]
    operands = args[1:] if negate else args
    if len(operands) != _UNARY_TEST_WORDS or operands[0] not in _TEST_OPERATORS:
        raise _UnsupportedError
    check, path = _TEST_OPERATORS[operands[0]], Path(operands[1])

    def test(_out: list[str], _errors: list[str]) -> int:
        return 0 if check(path) != negate else 1

    return test


def _bracket(args: list[str]) -> Callable[[list[str], list[str]], int]:
    if args[-1:] != ["]"]:
        raise _UnsupportedError
    return _test(args[:-1])


def _two_paths(args: list[str]) -> tuple[Path, Path]:
    _, paths = _paths(args, flags=frozenset())
    if len(paths) != _SOURCE_AND_DESTINATION:
        raise _UnsupportedError
    return paths[0], paths[1]


def _mv(args: list[str]) -> Callable[[list[str], list[str]], int]:
    source, destination = _two_paths(args)

    def mv(_out: list[str], errors: list[str]) -> int:
        target = destination / source.name if destination.is_dir() else destination
        try:
            source.replace(target)
        except OSError as error:
            return _fail(errors, "mv", str(source), error)
        return 0

    return mv


def _ln(args: list[str]) -> Callable[[list[str], list[str]], int]:
    source, destination = _two_paths(args)

    def ln(_out: list[str], errors: list[str]) -> int:
        target = destination / source.name if destination.is_dir() else destination
        try:
            os.link(source, target)
        except OSError as error:
            return _fail(errors, "ln", str(target), error)
        return 0

    return ln


def _rm(args: list[str]) -> Callable[[list[str], list[str]], int]:
    given, paths = _paths(args, flags=frozenset({"-f"}))
    if not paths:
        raise _UnsupportedError

    def rm(_out: list[str], errors: list[str]) -> int:
        status = 0
        for path in paths:
            try:
                path.unlink()
            except FileNotFoundError as error:
                if "-f" not in given:
                    status = _fail(errors, "rm", str(path), error)
            except OSError as error:
                status = _fail(errors, "rm", str(path), error)
        return status

    return rm


def _sync(_args: list[str]) -> Callable[[list[str], list[str]], int]:
    def sync(_out: list[str], _errors: list[str]) -> int:
        return 0

    return sync
