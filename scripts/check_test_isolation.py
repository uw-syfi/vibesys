#!/usr/bin/env python3
"""Fail when tests add patching, mocking, waiting, or private library imports.

The testing policy is: tests exercise public APIs, use in-memory Fakes instead
of monkeypatch or mock, and never synchronize on a sleep, a timeout, or the
clock. Ruff cannot count these per file, so this script scans test modules with
the Python AST and ratchets the worst offenders.

The check is a shrink-only ratchet. Existing violations are recorded per
(file, rule) in a JSONL baseline, so the check passes today and fails the
moment a file gains a site. Removing sites is always allowed; the script prints
the entries worth tightening and refuses to let an entry that has dropped to
zero linger.

Rules (each site counts once; verdict rules count a line once):

    patch            `.setattr`, `.setitem`, `.delattr`, `.delitem` on the
                     `monkeypatch` fixture or on a `pytest.MonkeyPatch()`
                     instance. `setenv`, `delenv`, and `chdir` are allowed:
                     they set test inputs.
    mock             An import of `unittest.mock` or `pytest_mock`, a `mocker`
                     parameter, a call on `mocker`, and any use of `patch`,
                     `patch.object`, `patch.dict`, `MagicMock`, `Mock`,
                     `AsyncMock`, or `create_autospec` imported from those
                     modules.
    sleep            `time.sleep(...)` and `asyncio.sleep(...)` (also a bare
                     `sleep` imported from either), except `asyncio.sleep(0)`,
                     which is a plain scheduler yield.
    timeout_verdict  A timeout or a clock reading that decides the test's
                     verdict. See "What counts as a timeout verdict" below.
    private_import   Under `libs/<name>/tests/` and `sdk/<name>/tests/` only:
                     an import of the library's own package (the directory
                     under `src/`) that is not `<pkg>.api` or a submodule of
                     it. `import <pkg>` and `from <pkg> import x` count.
    wall_clock_sync  In `*.test.ts`, a call to the ambient `setTimeout`
                     scheduler, including optional calls and aliases imported
                     from Node's timer modules. A call through an injected
                     object, such as `scheduler.setTimeout(...)`, is allowed:
                     that object can be a deterministic Fake.

A site is exempt when its line, or a standalone comment on the line directly
above it, carries `# test-isolation: <reason>` in Python or
`// test-isolation: <reason>` in TypeScript, with a non-empty reason. An
exemption with an empty reason is itself a violation (`empty_exemption`) that
is never baselinable.

What counts as TypeScript wall-clock synchronization
----------------------------------------------------

The TypeScript arm is a lexical call-expression scanner, not a text search. It
skips comments and quoted strings, understands escaped characters, and scans
`${...}` expressions while treating a template's raw text as string data. It
counts direct, grouped, computed, and optional `setTimeout` calls, explicit
ambient receivers such as `globalThis` and Node's `global`, and aliases from
static or simple declarative dynamic imports of the `node:timers` and
`node:timers/promises` module families. An unrecognized dynamic timer import
counts at its import site instead of becoming an escape hatch. A method call
on another receiver is not ambient and is therefore allowed: tests can inject
and drive a Fake scheduler through the same public interface production uses.
`setInterval` is not included: the existing launcher fixtures use it only to
keep child processes alive, not to decide when an assertion may run, and #866
owns replacing those fixtures.
Likewise, timer calls in raw launcher template text belong to the generated
child process, not the outer TypeScript test program, and remain owned by #866.

This rule covers wall-clock budgets visible in test source. It cannot see a
dependency's hidden clock, such as OpenTUI's renderer clock; #1095 tracks that
separate injection boundary.

What counts as a timeout verdict
--------------------------------

`threading.Event.wait(timeout=T)` is not decidable from the call site alone:
the same expression is a deadlock guard when a Fake sets the event and a
synchronization budget when real timers do. The criterion the rule uses is
what the expiry can do:

    A timeout is a deadlock guard when expiry can only fail the test on the
    spot, and a synchronization budget when expiry is consumed as a value.

A guard is monotone in its timeout: raising T can never turn a pass into a
failure, so the value is not load-bearing and no verdict rests on the clock.
A budget is not monotone, so the value is the verdict. The counted shapes are:

    assert not ev.wait(timeout=T)   Expiry is the success condition, so
                                    elapsed time is the whole evidence. Also
                                    `... is False` and `... == False`.
    return ev.wait(timeout=T)       The expiry boolean leaves the function as
                                    its value, invisible at the call site.
    with pytest.raises(TimeoutError): await asyncio.wait_for(...)
                                    Expiry is the expected outcome. Also
                                    `contextlib.suppress` and a `try` whose
                                    `except` names a timeout-expiry type
                                    (`TimeoutError`, `TimeoutExpired`,
                                    `queue.Empty`, `socket.timeout`).
    while time.monotonic() < deadline:
                                    A wall-clock deadline loop. Also a clock
                                    reading compared inside an `assert` or an
                                    `if` condition, which is asserting on
                                    elapsed time.

Which waits the shapes look at follows from how each reports expiry. The first
two shapes need a wait that hands expiry back as a value, so they look at
`wait`, `wait_for`, and `acquire` with a finite `timeout=` keyword
(`threading.Event.wait`, `Condition.wait_for`, `Lock.acquire`). The third needs
a wait that raises on expiry, so it looks at `get`, `result`, `communicate`,
and `recv` with a finite `timeout=`, plus `asyncio.wait_for`,
`asyncio.timeout`, `asyncio.timeout_at`, and `socket.settimeout`. A clock
reading is `monotonic`, `time`, `perf_counter`, `process_time` and their `_ns`
forms, on the `time` module or imported from it.

Deliberately not counted, so that the gate stays worth reading:

    - `assert ev.wait(timeout=T)`: a guard by the criterion above. This is the
      bulk of the timed waits in this repository, and flagging them would
      baseline hundreds of legitimate sites.
    - A bare `sock.settimeout(T)` or `await asyncio.wait_for(x, timeout=T)`
      whose expiry nobody catches: expiry fails the test with an error instead
      of producing a verdict. `return proc.communicate(timeout=T)` is the same
      case, since `communicate` raises rather than returning the expiry.
    - A production timeout passed into the system under test, or a timeout
      constant asserted on. Those are inputs, not synchronization.
    - `timeout=None`, which arms no deadline.
    - Dataflow through a local: `ok = ev.wait(timeout=T)` then `assert not ok`,
      and `elapsed = time.monotonic() - start` then `assert elapsed < T`. The
      rule reads one expression at a time and does no dataflow analysis, so it
      errs toward catching too little.
    - `datetime.now()` and other non-`time` clocks.

Configuration lives in `pyproject.toml` under `[tool.vibesys.test_isolation]`:

    roots     -- glob patterns of directories scanned for `*.py` and
                 `*.test.ts`. `__pycache__` and `fixtures` directories are
                 skipped.
    baseline  -- repo-relative JSONL path, one `{"path", "rule", "count"}`
                 entry per line (optional, default
                 `tests/quality/isolation_baseline.jsonl`).

Conditions that fail:

    1. A (file, rule) count exceeds its baseline, or has no baseline entry.
    2. A baseline entry is stale: the file is gone or its count is 0.
    3. An empty `# test-isolation:` exemption.

`--write` regenerates the baseline from the current tree. It refuses to grow a
count or add an entry unless the baseline does not exist yet, so it can only
shrink or bootstrap.

Usage:
    uv run python scripts/check_test_isolation.py
    uv run python scripts/check_test_isolation.py --write
    uv run python scripts/check_test_isolation.py --root /path/to/repo.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import sys
import tokenize
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

DEFAULT_PYPROJECT = Path("pyproject.toml")
DEFAULT_BASELINE = "tests/quality/isolation_baseline.jsonl"
SKIPPED_DIR_NAMES = frozenset(
    {
        "__pycache__",
        "build",
        "dist",
        "fixtures",
        "generated",
        "node_modules",
        "vendor",
    }
)

RULE_PATCH = "patch"
RULE_MOCK = "mock"
RULE_SLEEP = "sleep"
RULE_TIMEOUT_VERDICT = "timeout_verdict"
RULE_PRIVATE_IMPORT = "private_import"
RULE_WALL_CLOCK_SYNC = "wall_clock_sync"
RULE_EMPTY_EXEMPTION = "empty_exemption"
BASELINE_RULES = frozenset(
    {
        RULE_PATCH,
        RULE_MOCK,
        RULE_SLEEP,
        RULE_TIMEOUT_VERDICT,
        RULE_PRIVATE_IMPORT,
        RULE_WALL_CLOCK_SYNC,
    }
)

PATCH_METHODS = frozenset({"setattr", "setitem", "delattr", "delitem"})
MOCK_MODULES = ("unittest.mock", "pytest_mock")
MOCK_SYMBOLS = frozenset({"patch", "MagicMock", "Mock", "AsyncMock", "create_autospec"})
SLEEP_MODULES = frozenset({"time", "asyncio"})

TIMEOUT_KEYWORD = "timeout"
REPORTING_WAIT_METHODS = frozenset({"wait", "wait_for", "acquire"})
RAISING_WAIT_METHODS = frozenset({"get", "result", "communicate", "recv"})
DEADLINE_SETTER = "settimeout"
ASYNCIO_TIMEOUT_HELPERS = frozenset({"wait_for", "timeout", "timeout_at"})
EXPIRY_EXCEPTIONS = frozenset({"TimeoutError", "TimeoutExpired", "Empty", "timeout"})
EXPIRY_CONTEXT_FUNCTIONS = frozenset({"raises", "suppress"})
CLOCK_FUNCTIONS = frozenset(
    {
        "monotonic",
        "monotonic_ns",
        "time",
        "time_ns",
        "perf_counter",
        "perf_counter_ns",
        "process_time",
        "process_time_ns",
    }
)
PACKAGE_API = "api"
LIBRARY_TESTS_RE = re.compile(r"^(?:libs|sdk)/([^/]+)/tests/")
EXEMPTION_RE = re.compile(r"#\s*test-isolation:(.*)$")
TYPESCRIPT_EXEMPTION_RE = re.compile(r"//\s*test-isolation:(.*)$")
AMBIENT_TIMER_RECEIVERS = frozenset({"global", "globalThis", "window", "self"})
NODE_TIMER_MODULES = frozenset({"node:timers", "node:timers/promises", "timers", "timers/promises"})
HEXADECIMAL_DIGITS = frozenset("0123456789abcdefABCDEF")
HEX_ESCAPE_DIGITS = 2
UNICODE_ESCAPE_DIGITS = 4
MAX_BRACED_UNICODE_DIGITS = 6
MAX_UNICODE_CODE_POINT = 0x10FFFF
DYNAMIC_DECLARATION_PREFIX_SIZE = 2

REMEDIATION = (
    "Use a Fake or injectable seam, or add a reviewed "
    "`# test-isolation: <reason>` / `// test-isolation: <reason>` exemption."
)

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_TOOL_ERROR = 2

Key = tuple[str, str]


@dataclass(frozen=True)
class Config:
    """Resolved `[tool.vibesys.test_isolation]` settings."""

    roots: tuple[str, ...]
    baseline: str


@dataclass
class Scan:
    """Per-(file, rule) site counts plus empty exemption comments."""

    counts: dict[Key, int] = field(default_factory=dict)
    empty_exemptions: list[tuple[str, int]] = field(default_factory=list)


@dataclass(frozen=True)
class Comparison:
    """Outcome of comparing current counts with the baseline."""

    failures: list[str]
    stale: list[str]
    tightenable: list[str]


class ConfigError(Exception):
    """The configuration or baseline is missing or malformed."""

    @classmethod
    def unreadable(cls, path: Path, error: OSError) -> ConfigError:
        """Describe a file that could not be read."""
        return cls(f"{path}: cannot be read ({error})")

    @classmethod
    def invalid_toml(cls, path: Path, error: tomllib.TOMLDecodeError) -> ConfigError:
        """Describe invalid TOML in the project configuration file."""
        return cls(f"{path}: is not valid TOML ({error})")

    @classmethod
    def missing_key(cls, path: Path, key: KeyError) -> ConfigError:
        """Describe a missing configuration key."""
        return cls(f"{path}: missing [tool.vibesys.test_isolation] key {key}")

    @classmethod
    def bad_baseline_entry(cls, path: Path, line_number: int, problem: str) -> ConfigError:
        """Describe a malformed baseline line."""
        return cls(f"{path}:{line_number}: {problem}")

    @classmethod
    def unparsable(cls, path: str, error: SyntaxError) -> ConfigError:
        """Describe a test file that is not valid Python."""
        return cls(f"{path}: cannot be parsed ({error.msg}, line {error.lineno})")


def load_config(pyproject_path: Path) -> Config:
    """Read the scan roots and baseline path from ``pyproject.toml``.

    Raises:
        ConfigError: If the section is absent or a required key is missing.
    """
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError.unreadable(pyproject_path, exc) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError.invalid_toml(pyproject_path, exc) from exc
    try:
        section = data["tool"]["vibesys"]["test_isolation"]
        roots = tuple(str(entry) for entry in section["roots"])
    except KeyError as exc:
        raise ConfigError.missing_key(pyproject_path, exc) from exc
    return Config(roots=roots, baseline=str(section.get("baseline", DEFAULT_BASELINE)))


def load_baseline(path: Path) -> dict[Key, int]:
    """Read the JSONL baseline into a (path, rule) -> count map.

    Raises:
        ConfigError: If the file cannot be read or an entry is malformed.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ConfigError.unreadable(path, exc) from exc
    baseline: dict[Key, int] = {}
    for line_number, line in enumerate(lines, start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, count = _parse_baseline_line(path, line_number, line)
        if key in baseline:
            raise ConfigError.bad_baseline_entry(path, line_number, f"duplicate entry {key}")
        baseline[key] = count
    return baseline


def _parse_baseline_line(path: Path, line_number: int, line: str) -> tuple[Key, int]:
    try:
        value: object = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ConfigError.bad_baseline_entry(
            path, line_number, f"invalid JSON ({exc.msg})"
        ) from exc
    if not isinstance(value, dict):
        raise ConfigError.bad_baseline_entry(path, line_number, "entry must be a JSON object")
    file_path, rule, count = value.get("path"), value.get("rule"), value.get("count")
    if not isinstance(file_path, str) or not file_path:
        raise ConfigError.bad_baseline_entry(path, line_number, "path must be a non-empty string")
    if rule not in BASELINE_RULES:
        raise ConfigError.bad_baseline_entry(
            path, line_number, f"rule must be one of {sorted(BASELINE_RULES)}"
        )
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ConfigError.bad_baseline_entry(path, line_number, "count must be an integer >= 1")
    return (file_path, rule), count


def format_baseline(counts: dict[Key, int]) -> str:
    """Render counts as sorted JSONL, one entry per line."""
    lines = [
        json.dumps({"path": path, "rule": rule, "count": count})
        for (path, rule), count in sorted(counts.items())
        if count > 0
    ]
    return "".join(f"{line}\n" for line in lines)


def _comment_lines(source: str) -> tuple[dict[int, str], set[int]]:
    """Return line -> exemption reason for every `# test-isolation:` comment.

    The second value is the set of lines where the comment stands alone, since
    only a standalone comment can exempt the line below it.
    """
    reasons: dict[int, str] = {}
    standalone: set[int] = set()
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError):
        return reasons, standalone
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        match = EXEMPTION_RE.search(token.string)
        if match is None:
            continue
        line = token.start[0]
        reasons[line] = match.group(1).strip()
        if not token.line[: token.start[1]].strip():
            standalone.add(line)
    return reasons, standalone


@dataclass(frozen=True)
class _TypeScriptToken:
    """One identifier or punctuation token relevant to the timer rule."""

    value: str
    line: int
    quoted: bool = False


def _typescript_tokens(
    source: str, *, start_line: int = 1
) -> tuple[list[_TypeScriptToken], dict[int, str], set[int]]:
    """Lex TypeScript without mistaking prose for executable timer calls.

    Template interpolation expressions are code; raw template text and
    ordinary quoted strings remain data. The lexer intentionally emits only
    identifiers and punctuation: the rule needs call shape and receiver
    identity, not a TypeScript type checker.
    """
    return _TypeScriptLexer(source, start_line).scan()


class _TypeScriptLexer:
    """Small lexer for the TypeScript timer rule and its exemptions."""

    def __init__(self, source: str, start_line: int) -> None:
        self.source = source
        self.index = 0
        self.line = start_line
        self.line_start = 0
        self.tokens: list[_TypeScriptToken] = []
        self.reasons: dict[int, str] = {}
        self.standalone: set[int] = set()

    def scan(self) -> tuple[list[_TypeScriptToken], dict[int, str], set[int]]:
        """Consume the source and return its relevant lexical facts."""
        while self.index < len(self.source):
            char = self.source[self.index]
            if char == "\n":
                self._newline()
            elif char.isspace():
                self.index += 1
            elif self._slash():
                continue
            elif char in {"'", '"'}:
                self._quoted(char)
            elif char == "`":
                self._template()
            elif char.isalpha() or char in {"_", "$"}:
                self._identifier()
            else:
                self.tokens.append(_TypeScriptToken(char, self.line))
                self.index += 1
        return self.tokens, self.reasons, self.standalone

    def _newline(self) -> None:
        self.line += 1
        self.index += 1
        self.line_start = self.index

    def _slash(self) -> bool:
        """Consume a comment or regex beginning here, if this slash is one."""
        if self.source[self.index] != "/":
            return False
        following = self.source[self.index + 1] if self.index + 1 < len(self.source) else ""
        if following == "/":
            self._line_comment()
        elif following == "*":
            self.index, self.line, self.line_start = _skip_typescript_block_comment(
                self.source, self.index, self.line, self.line_start
            )
        elif _typescript_regex_can_start(self.tokens):
            self.index, self.line, self.line_start = _skip_typescript_regex(
                self.source, self.index, self.line, self.line_start
            )
        else:
            return False
        return True

    def _line_comment(self) -> None:
        end = self.source.find("\n", self.index + 2)
        if end < 0:
            end = len(self.source)
        match = TYPESCRIPT_EXEMPTION_RE.search(self.source[self.index : end])
        if match is not None:
            self.reasons[self.line] = match.group(1).strip()
            if not self.source[self.line_start : self.index].strip():
                self.standalone.add(self.line)
        self.index = end

    def _template(self) -> None:
        end, end_line, end_line_start, expressions = _typescript_template_expressions(
            self.source, self.index, self.line, self.line_start
        )
        for expression, start_line in expressions:
            tokens, reasons, standalone = _typescript_tokens(expression, start_line=start_line)
            self.tokens.extend(tokens)
            self.reasons.update(reasons)
            self.standalone.update(standalone)
        self.index, self.line, self.line_start = end, end_line, end_line_start

    def _identifier(self) -> None:
        end = self.index + 1
        while end < len(self.source) and (
            self.source[end].isalnum() or self.source[end] in {"_", "$"}
        ):
            end += 1
        self.tokens.append(_TypeScriptToken(self.source[self.index : end], self.line))
        self.index = end

    def _quoted(self, quote: str) -> None:
        """Record a quoted literal as one token without scanning its contents."""
        start = self.index
        start_line = self.line
        self.index, self.line, self.line_start = _skip_typescript_quoted(
            self.source, self.index, self.line, self.line_start, quote
        )
        closed = (
            self.index <= len(self.source) and self.source[self.index - 1 : self.index] == quote
        )
        end = self.index - 1 if closed else self.index
        raw = self.source[start + 1 : end]
        self.tokens.append(
            _TypeScriptToken(_typescript_cooked_string(raw), start_line, quoted=True)
        )


def _typescript_hex_escape(raw: str, start: int, length: int) -> tuple[str, int] | None:
    """Decode a fixed-width hexadecimal escape beginning at ``start``."""
    digits = raw[start : start + length]
    if len(digits) != length or any(char not in HEXADECIMAL_DIGITS for char in digits):
        return None
    return chr(int(digits, 16)), start + length


def _typescript_unicode_escape(raw: str, start: int) -> tuple[str, int] | None:
    r"""Decode the body after a JavaScript ``\u`` escape."""
    if raw[start : start + 1] != "{":
        return _typescript_hex_escape(raw, start, UNICODE_ESCAPE_DIGITS)
    end = raw.find("}", start + 1)
    digits = raw[start + 1 : end] if end >= 0 else ""
    if (
        not 1 <= len(digits) <= MAX_BRACED_UNICODE_DIGITS
        or any(char not in HEXADECIMAL_DIGITS for char in digits)
        or int(digits, 16) > MAX_UNICODE_CODE_POINT
    ):
        return None
    return chr(int(digits, 16)), end + 1


def _typescript_cooked_escape(raw: str, slash: int) -> tuple[str, int] | None:
    """Decode one JavaScript string escape at ``slash``."""
    escapes = {
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
        "0": "\0",
    }
    if slash + 1 >= len(raw):
        return None
    escape = raw[slash + 1]
    if escape == "\n":
        return "", slash + 2
    if escape == "\r":
        following = slash + 2
        return "", following + (following < len(raw) and raw[following] == "\n")
    if escape in escapes:
        return escapes[escape], slash + 2
    if escape in {"x", "u"}:
        return (
            _typescript_hex_escape(raw, slash + 2, HEX_ESCAPE_DIGITS)
            if escape == "x"
            else _typescript_unicode_escape(raw, slash + 2)
        )
    return escape, slash + 2


def _typescript_cooked_string(raw: str) -> str:
    """Return the JavaScript value of a valid quoted-string body.

    The scanner only compares string values used as module specifiers and
    computed member names. Returning the raw text for an invalid escape keeps
    malformed source from accidentally matching one of those contracts.
    """
    cooked: list[str] = []
    index = 0
    while index < len(raw):
        if raw[index] != "\\":
            cooked.append(raw[index])
            index += 1
            continue
        decoded = _typescript_cooked_escape(raw, index)
        if decoded is None:
            return raw
        value, index = decoded
        cooked.append(value)
    return "".join(cooked)


def _skip_typescript_block_comment(
    source: str, index: int, line: int, line_start: int
) -> tuple[int, int, int]:
    """Return the position after one block comment and its line state."""
    end = source.find("*/", index + 2)
    if end < 0:
        end = len(source) - 2
    index = min(end + 2, len(source))
    consumed = source[line_start:index]
    line += consumed.count("\n")
    newline = source.rfind("\n", 0, index)
    return index, line, newline + 1 if newline >= 0 else line_start


def _skip_typescript_quoted(
    source: str, index: int, line: int, line_start: int, quote: str
) -> tuple[int, int, int]:
    """Return the position after one single- or double-quoted literal."""
    index += 1
    escaped = False
    while index < len(source):
        char = source[index]
        if char == "\n":
            line += 1
            line_start = index + 1
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == quote:
            return index + 1, line, line_start
        index += 1
    return index, line, line_start


def _typescript_template_expressions(
    source: str, index: int, line: int, line_start: int
) -> tuple[int, int, int, list[tuple[str, int]]]:
    """Skip template data and return its `${...}` expression sources."""
    index += 1
    escaped = False
    expressions: list[tuple[str, int]] = []
    while index < len(source):
        char = source[index]
        if char == "\n":
            line += 1
            line_start = index + 1
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "`":
            return index + 1, line, line_start, expressions
        elif char == "$" and index + 1 < len(source) and source[index + 1] == "{":
            expression_start = index + 2
            expression_line = line
            end, line, line_start = _typescript_expression_end(
                source, expression_start, line, line_start
            )
            expressions.append((source[expression_start:end], expression_line))
            index = end
        index += 1
    return len(source), line, line_start, expressions


def _typescript_expression_end(
    source: str, index: int, line: int, line_start: int
) -> tuple[int, int, int]:
    """Find the balanced `}` ending one template interpolation."""
    depth = 1
    while index < len(source):
        char = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if char == "\n":
            line += 1
            line_start = index + 1
        elif char in {"'", '"'}:
            index, line, line_start = _skip_typescript_quoted(source, index, line, line_start, char)
            continue
        elif char == "`":
            index, line, line_start, _ = _typescript_template_expressions(
                source, index, line, line_start
            )
            continue
        elif char == "/" and following == "/":
            end = source.find("\n", index + 2)
            index = len(source) if end < 0 else end
            continue
        elif char == "/" and following == "*":
            index, line, line_start = _skip_typescript_block_comment(
                source, index, line, line_start
            )
            continue
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index, line, line_start
        index += 1
    return len(source), line, line_start


def _typescript_regex_can_start(tokens: list[_TypeScriptToken]) -> bool:
    """Use the preceding token to distinguish a regex literal from division."""
    if not tokens:
        return True
    return tokens[-1].value in {
        "(",
        "[",
        "{",
        "=",
        ",",
        ":",
        ";",
        "!",
        "&",
        "|",
        "?",
        "return",
        "case",
        "throw",
    }


def _skip_typescript_regex(
    source: str, index: int, line: int, line_start: int
) -> tuple[int, int, int]:
    """Return the position after a JavaScript regex literal and its flags."""
    index += 1
    escaped = False
    in_class = False
    while index < len(source):
        char = source[index]
        if char == "\n":
            return index, line, line_start
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            index += 1
            while index < len(source) and source[index].isalpha():
                index += 1
            return index, line, line_start
        index += 1
    return index, line, line_start


def _typescript_dynamic_import_equals(
    tokens: list[_TypeScriptToken], import_index: int
) -> int | None:
    """Return the assignment token for a simple complete dynamic import."""
    if (
        import_index + 3 >= len(tokens)
        or tokens[import_index + 1].value != "("
        or not tokens[import_index + 2].quoted
        or tokens[import_index + 3].value != ")"
    ):
        return None
    equals = import_index - 1
    if equals < 0 or tokens[equals].value != "await":
        return None
    equals -= 1
    return equals if equals >= 0 and tokens[equals].value == "=" else None


def _typescript_destructured_timer_aliases(
    tokens: list[_TypeScriptToken], equals: int
) -> set[str] | None:
    """Return `setTimeout` aliases from a flat declaration pattern."""
    opening = equals - 2
    while opening >= 0 and tokens[opening].value not in {"{", ";"}:
        opening -= 1
    if (
        opening <= 0
        or tokens[opening].value != "{"
        or tokens[opening - 1].value
        not in {
            "const",
            "let",
            "var",
        }
    ):
        return None

    clause = tokens[opening + 1 : equals - 1]
    parts: list[list[_TypeScriptToken]] = [[]]
    for token in clause:
        parts.append([]) if token.value == "," else parts[-1].append(token)
    direct: set[str] = set()
    for part in parts:
        match part:
            case []:
                continue
            case [imported] if not imported.quoted:
                local = imported
            case [imported, separator, local] if (
                not imported.quoted and separator.value == ":" and not local.quoted
            ):
                pass
            case _:
                return None
        if imported.value == "setTimeout":
            direct.add(local.value)
    return direct


def _typescript_dynamic_timer_binding(
    tokens: list[_TypeScriptToken], import_index: int
) -> tuple[set[str], set[str]] | None:
    """Return aliases from one supported declarative dynamic import.

    Supported forms bind the import directly to a namespace identifier or a
    flat object pattern. More elaborate expressions are rejected by the
    caller rather than guessed at, because guessing could silently miss the
    identifier eventually used to call the real scheduler.
    """
    equals = _typescript_dynamic_import_equals(tokens, import_index)
    if equals is None or equals < DYNAMIC_DECLARATION_PREFIX_SIZE:
        return None
    declaration = tokens[equals - 1]
    if tokens[equals - 2].value in {"const", "let", "var"} and not declaration.quoted:
        return set(), {declaration.value}
    if declaration.value != "}":
        return None
    direct = _typescript_destructured_timer_aliases(tokens, equals)
    return (direct, set()) if direct is not None else None


def _typescript_static_timer_binding(
    tokens: list[_TypeScriptToken], import_index: int
) -> tuple[set[str], set[str]] | None:
    """Return aliases from one static Node timer import."""
    statement_end = next(
        (
            offset
            for offset in range(import_index + 1, len(tokens))
            if tokens[offset].value == ";"
            or (tokens[offset].value == "import" and not tokens[offset].quoted)
        ),
        len(tokens),
    )
    statement = tokens[import_index + 1 : statement_end]
    from_index = next(
        (offset for offset, item in enumerate(statement) if item.value == "from"), None
    )
    if from_index is None or from_index + 1 >= len(statement):
        return None
    module = statement[from_index + 1]
    if not module.quoted or module.value not in NODE_TIMER_MODULES:
        return None
    clause = statement[:from_index]
    direct: set[str] = set()
    receivers: set[str] = set()
    if clause and clause[0].value not in {"{", "*", "type"}:
        receivers.add(clause[0].value)
    for offset, item in enumerate(clause):
        if item.value == "*" and offset + 2 < len(clause) and clause[offset + 1].value == "as":
            receivers.add(clause[offset + 2].value)
        if item.value != "setTimeout":
            continue
        if offset + 2 < len(clause) and clause[offset + 1].value == "as":
            direct.add(clause[offset + 2].value)
        else:
            direct.add(item.value)
    return direct, receivers


def _typescript_timer_imports(
    tokens: list[_TypeScriptToken],
) -> tuple[frozenset[str], frozenset[str], tuple[int, ...]]:
    """Return Node timer aliases and rejected dynamic-import lines."""
    direct: set[str] = {"setTimeout"}
    receivers: set[str] = set(AMBIENT_TIMER_RECEIVERS)
    rejected_dynamic: list[int] = []
    for index, token in enumerate(tokens):
        if (
            token.value != "import"
            or token.quoted
            or (index > 0 and tokens[index - 1].value == ".")
        ):
            continue
        if index + 2 < len(tokens) and tokens[index + 1].value == "(":
            module = tokens[index + 2]
            if not module.quoted or module.value not in NODE_TIMER_MODULES:
                continue
            binding = _typescript_dynamic_timer_binding(tokens, index)
            if binding is None:
                rejected_dynamic.append(token.line)
                continue
            imported_direct, imported_receivers = binding
            direct.update(imported_direct)
            receivers.update(imported_receivers)
            continue
        binding = _typescript_static_timer_binding(tokens, index)
        if binding is None:
            continue
        imported_direct, imported_receivers = binding
        direct.update(imported_direct)
        receivers.update(imported_receivers)
    return frozenset(direct), frozenset(receivers), tuple(rejected_dynamic)


def _typescript_group_can_start(tokens: list[_TypeScriptToken], opening: int) -> bool:
    """Whether ``(`` begins grouping rather than an argument list."""
    if opening == 0:
        return True
    return tokens[opening - 1].value in {
        "(",
        "[",
        "{",
        "=",
        ",",
        ":",
        ";",
        "!",
        "~",
        "+",
        "-",
        "*",
        "/",
        "%",
        "&",
        "|",
        "^",
        "?",
        "<",
        ">",
        "return",
        "case",
        "throw",
        "yield",
        "await",
    }


def _typescript_matching_opening_parenthesis(
    tokens: list[_TypeScriptToken], closing: int
) -> int | None:
    """Return the opening parenthesis paired with ``closing``."""
    depth = 0
    for index in range(closing, -1, -1):
        if tokens[index].value == ")":
            depth += 1
        elif tokens[index].value == "(":
            depth -= 1
            if depth == 0:
                return index
    return None


def _typescript_receiver_identifier(
    tokens: list[_TypeScriptToken], end: int
) -> tuple[int, str] | None:
    """Return a receiver identifier and span start, unwrapping grouping."""
    if end <= 0:
        return None
    candidate = end - 1
    if tokens[candidate].value != ")":
        return None if tokens[candidate].quoted else (candidate, tokens[candidate].value)
    opening = _typescript_matching_opening_parenthesis(tokens, candidate)
    if opening is None or not _typescript_group_can_start(tokens, opening):
        return None
    inner = _typescript_receiver_identifier(tokens, candidate)
    if inner is None or inner[0] != opening + 1:
        return None
    return opening, inner[1]


def _typescript_computed_timer_reference(
    tokens: list[_TypeScriptToken],
    index: int,
    receivers: frozenset[str],
) -> tuple[int, int] | None:
    """Return the span of a computed ambient timer member."""
    token = tokens[index]
    if (
        token.value != "setTimeout"
        or index == 0
        or index + 1 >= len(tokens)
        or tokens[index - 1].value != "["
        or tokens[index + 1].value != "]"
    ):
        return None
    bracket = index - 1
    receiver_end = bracket
    if [item.value for item in tokens[max(0, bracket - 2) : bracket]] == ["?", "."]:
        receiver_end -= 2
    receiver = _typescript_receiver_identifier(tokens, receiver_end)
    if receiver is None or receiver[1] not in receivers:
        return None
    return receiver[0], index + 2


def _typescript_named_timer_reference(
    tokens: list[_TypeScriptToken],
    index: int,
    direct: frozenset[str],
    receivers: frozenset[str],
) -> tuple[int, int] | None:
    """Return the span of a direct or dotted timer reference."""
    token = tokens[index]

    if token.value in direct and (index == 0 or tokens[index - 1].value != "."):
        return index, index + 1
    if token.value != "setTimeout" or index == 0 or tokens[index - 1].value != ".":
        return None
    receiver_end = index - 1
    if receiver_end > 0 and tokens[receiver_end - 1].value == "?":
        receiver_end -= 1
    receiver = _typescript_receiver_identifier(tokens, receiver_end)
    if receiver is None or receiver[1] not in receivers:
        return None
    return receiver[0], index + 1


def _typescript_timer_reference(
    tokens: list[_TypeScriptToken],
    index: int,
    direct: frozenset[str],
    receivers: frozenset[str],
) -> tuple[int, int] | None:
    """Return the half-open token span of a timer reference at ``index``."""
    if tokens[index].quoted:
        return _typescript_computed_timer_reference(tokens, index, receivers)
    return _typescript_named_timer_reference(tokens, index, direct, receivers)


def _typescript_reference_is_called(tokens: list[_TypeScriptToken], start: int, end: int) -> bool:
    """Whether a recognized reference is invoked, allowing safe grouping."""
    while (
        start > 0
        and end < len(tokens)
        and tokens[start - 1].value == "("
        and tokens[end].value == ")"
        and _typescript_group_can_start(tokens, start - 1)
    ):
        start -= 1
        end += 1
    following = [item.value for item in tokens[end : end + 3]]
    return following[:1] == ["("] or following == ["?", ".", "("]


def _typescript_wall_clock_lines(tokens: list[_TypeScriptToken]) -> list[int]:
    """Return lines containing calls to ambient or Node-imported timers."""
    direct, receivers, rejected_dynamic = _typescript_timer_imports(tokens)
    lines = list(rejected_dynamic)
    for index, token in enumerate(tokens):
        reference = _typescript_timer_reference(tokens, index, direct, receivers)
        if reference is not None and _typescript_reference_is_called(tokens, *reference):
            lines.append(token.line)
    return lines


class _Sites:
    """Collect (rule, line) sites from one module."""

    def __init__(self, library_packages: frozenset[str]) -> None:
        self.library_packages = library_packages
        self.sites: list[tuple[str, int]] = []
        self.monkeypatch_names: set[str] = {"monkeypatch"}
        self.mock_names: set[str] = set()
        self.mock_modules: set[str] = set()
        self.time_modules: set[str] = set()
        self.asyncio_modules: set[str] = set()
        self.sleep_names: dict[str, str] = {}
        self.clock_names: set[str] = set()
        self.asyncio_timeout_names: set[str] = set()
        self.timeout_lines: set[int] = set()

    def collect(self, tree: ast.Module) -> list[tuple[str, int]]:
        """Return every site in ``tree``."""
        self._bind_names(tree)
        self._collect_timeout_verdicts(tree)
        self._walk(tree)
        self.sites.extend((RULE_TIMEOUT_VERDICT, line) for line in sorted(self.timeout_lines))
        return self.sites

    def _bind_names(self, tree: ast.Module) -> None:
        """Resolve import aliases and MonkeyPatch instances before counting."""
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self._bind_import(node)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                self._bind_import_from(node)
            elif isinstance(node, ast.Assign | ast.AnnAssign):
                self._bind_assignment(node)
            elif isinstance(node, ast.With | ast.AsyncWith):
                for item in node.items:
                    if _is_monkeypatch_factory(item.context_expr) and isinstance(
                        item.optional_vars, ast.Name
                    ):
                        self.monkeypatch_names.add(item.optional_vars.id)

    def _bind_import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound = alias.asname or alias.name.split(".")[0]
            if alias.name == "time":
                self.time_modules.add(bound)
            elif alias.name == "asyncio":
                self.asyncio_modules.add(bound)
            elif alias.name == "unittest.mock" and alias.asname:
                self.mock_modules.add(alias.asname)
            elif alias.name == "unittest.mock" or alias.name.startswith("unittest.mock."):
                self.mock_modules.add("unittest.mock")
            elif alias.name.split(".")[0] == "pytest_mock":
                self.mock_modules.add(bound)

    def _bind_import_from(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            bound = alias.asname or alias.name
            if module in SLEEP_MODULES and alias.name == "sleep":
                self.sleep_names[bound] = module
            if module == "time" and alias.name in CLOCK_FUNCTIONS:
                self.clock_names.add(bound)
            if module == "asyncio" and alias.name in ASYNCIO_TIMEOUT_HELPERS:
                self.asyncio_timeout_names.add(bound)
            if _is_mock_module(module) and alias.name in MOCK_SYMBOLS:
                self.mock_names.add(bound)
            elif module == "unittest" and alias.name == "mock":
                self.mock_modules.add(bound)

    def _bind_assignment(self, node: ast.Assign | ast.AnnAssign) -> None:
        value = node.value
        if value is None or not _is_monkeypatch_factory(value):
            return
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        self.monkeypatch_names.update(t.id for t in targets if isinstance(t, ast.Name))

    def _walk(self, node: ast.AST) -> None:
        """Record sites at ``node``, then descend unless it was a mock reference."""
        if isinstance(node, ast.Import | ast.ImportFrom):
            self._record_import(node)
        elif isinstance(node, ast.arg):
            if node.arg == "mocker":
                self.sites.append((RULE_MOCK, node.lineno))
        elif isinstance(node, ast.Name):
            if node.id in self.mock_names:
                self.sites.append((RULE_MOCK, node.lineno))
        elif isinstance(node, ast.Attribute):
            if self._is_mock_symbol(node) or _root_name(node) == "mocker":
                self.sites.append((RULE_MOCK, node.lineno))
                return
        elif isinstance(node, ast.Call):
            self._call_sites(node)
        for child in ast.iter_child_nodes(node):
            self._walk(child)

    def _record_import(self, node: ast.Import | ast.ImportFrom) -> None:
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
            self._import_sites(node.lineno, modules, is_mock=any(map(_is_mock_module, modules)))
        elif node.level == 0:
            module = node.module or ""
            mock_submodule = module == "unittest" and any(a.name == "mock" for a in node.names)
            self._import_sites(
                node.lineno, [module], is_mock=_is_mock_module(module) or mock_submodule
            )

    def _import_sites(self, line: int, modules: list[str], *, is_mock: bool) -> None:
        """Record the mock and private-import sites of one import statement."""
        if is_mock:
            self.sites.append((RULE_MOCK, line))
        if any(self._is_private_library_module(module) for module in modules):
            self.sites.append((RULE_PRIVATE_IMPORT, line))

    def _is_private_library_module(self, module: str) -> bool:
        for package in self.library_packages:
            if module == package:
                return True
            if module.startswith(f"{package}."):
                public = f"{package}.{PACKAGE_API}"
                return module != public and not module.startswith(f"{public}.")
        return False

    def _call_sites(self, node: ast.Call) -> None:
        """Record monkeypatch mutations and sleeps."""
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in PATCH_METHODS
            and isinstance(func.value, ast.Name)
            and func.value.id in self.monkeypatch_names
        ):
            self.sites.append((RULE_PATCH, node.lineno))
            return
        module = self._sleep_module(func)
        if module is None:
            return
        if module == "asyncio" and _is_zero_argument(node):
            return
        self.sites.append((RULE_SLEEP, node.lineno))

    def _sleep_module(self, func: ast.expr) -> str | None:
        """Return "time" or "asyncio" when ``func`` is that module's sleep."""
        if isinstance(func, ast.Name):
            return self.sleep_names.get(func.id)
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "sleep"
            and isinstance(func.value, ast.Name)
        ):
            if func.value.id in self.time_modules:
                return "time"
            if func.value.id in self.asyncio_modules:
                return "asyncio"
        return None

    def _collect_timeout_verdicts(self, tree: ast.Module) -> None:
        """Record every timeout or clock reading that decides a verdict.

        At most one site per line, matching the granularity of the exemption
        comment that can waive it.
        """
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert):
                self._negated_waits(node.test, negated=False)
                self._clock_comparisons(node.test)
            elif isinstance(node, ast.While | ast.If):
                self._clock_comparisons(node.test)
            elif isinstance(node, ast.Return) and node.value is not None:
                self._returned_waits(node.value)
            elif isinstance(node, ast.With | ast.AsyncWith):
                if any(_is_expiry_context(item.context_expr) for item in node.items):
                    self._expected_expiries(node.body)
            elif isinstance(node, ast.Try | ast.TryStar) and _catches_expiry(node):
                self._expected_expiries(node.body)

    def _returned_waits(self, node: ast.expr) -> None:
        """Record waits whose expiry leaves the function as its value."""
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and self._is_reporting_wait(inner):
                self.timeout_lines.add(inner.lineno)

    def _expected_expiries(self, body: Sequence[ast.stmt]) -> None:
        """Record waits in ``body`` whose expiry the enclosing block expects."""
        for statement in body:
            for inner in ast.walk(statement):
                if isinstance(inner, ast.Call) and self._is_raising_wait(inner):
                    self.timeout_lines.add(inner.lineno)

    def _negated_waits(self, node: ast.expr, *, negated: bool) -> None:
        """Record timed waits in ``node`` whose expiry is the wanted outcome."""
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            self._negated_waits(node.operand, negated=not negated)
            return
        if isinstance(node, ast.Compare) and _has_false_operand(node):
            for operand in (node.left, *node.comparators):
                self._negated_waits(operand, negated=not negated)
            return
        if negated and isinstance(node, ast.Call) and self._is_reporting_wait(node):
            self.timeout_lines.add(node.lineno)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                self._negated_waits(child, negated=negated)

    def _clock_comparisons(self, node: ast.expr) -> None:
        """Record clock readings that ``node`` compares against a value."""
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Compare):
                continue
            for candidate in ast.walk(inner):
                if isinstance(candidate, ast.Call) and self._is_clock_reading(candidate):
                    self.timeout_lines.add(candidate.lineno)

    def _is_reporting_wait(self, call: ast.Call) -> bool:
        """Whether ``call`` waits and reports expiry as a falsy return value."""
        func = call.func
        if not isinstance(func, ast.Attribute) or self._is_asyncio_helper(func):
            return False
        return func.attr in REPORTING_WAIT_METHODS and _has_finite_timeout(call)

    def _is_raising_wait(self, call: ast.Call) -> bool:
        """Whether ``call`` waits on, or arms, a deadline whose expiry raises."""
        func = call.func
        if isinstance(func, ast.Name):
            return func.id in self.asyncio_timeout_names
        if not isinstance(func, ast.Attribute):
            return False
        if self._is_asyncio_helper(func):
            return True
        if func.attr == DEADLINE_SETTER:
            return bool(call.args) and not _is_none_literal(call.args[0])
        return func.attr in RAISING_WAIT_METHODS and _has_finite_timeout(call)

    def _is_asyncio_helper(self, func: ast.Attribute) -> bool:
        """Whether ``func`` is `asyncio.wait_for`, `.timeout`, or `.timeout_at`."""
        return (
            func.attr in ASYNCIO_TIMEOUT_HELPERS
            and isinstance(func.value, ast.Name)
            and func.value.id in self.asyncio_modules
        )

    def _is_clock_reading(self, call: ast.Call) -> bool:
        """Whether ``call`` reads the wall or monotonic clock."""
        func = call.func
        if isinstance(func, ast.Name):
            return func.id in self.clock_names
        return (
            isinstance(func, ast.Attribute)
            and func.attr in CLOCK_FUNCTIONS
            and isinstance(func.value, ast.Name)
            and func.value.id in self.time_modules
        )

    def _is_mock_symbol(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.mock_names
        if not isinstance(node, ast.Attribute):
            return False
        dotted = _dotted_name(node)
        if dotted is not None:
            for module in self.mock_modules:
                prefix = f"{module}."
                if dotted.startswith(prefix) and dotted[len(prefix) :].split(".")[0] in (
                    MOCK_SYMBOLS
                ):
                    return True
        return node.attr in {"object", "dict"} and self._is_mock_symbol(node.value)


def _is_zero_argument(call: ast.Call) -> bool:
    """Return whether ``call`` passes exactly the literal 0 (a scheduler yield)."""
    if len(call.args) != 1 or call.keywords:
        return False
    arg = call.args[0]
    return isinstance(arg, ast.Constant) and arg.value == 0 and not isinstance(arg.value, bool)


def _is_none_literal(expr: ast.expr) -> bool:
    return isinstance(expr, ast.Constant) and expr.value is None


def _has_finite_timeout(call: ast.Call) -> bool:
    """Return whether ``call`` passes an explicit `timeout=` other than None."""
    return any(
        keyword.arg == TIMEOUT_KEYWORD and not _is_none_literal(keyword.value)
        for keyword in call.keywords
    )


def _has_false_operand(node: ast.Compare) -> bool:
    """Return whether ``node`` compares something against the literal False."""
    return any(
        isinstance(operand, ast.Constant) and operand.value is False
        for operand in (node.left, *node.comparators)
    )


def _is_expiry_exception(expr: ast.expr) -> bool:
    """Return whether ``expr`` names a timeout-expiry exception type."""
    if isinstance(expr, ast.Name):
        return expr.id in EXPIRY_EXCEPTIONS
    if isinstance(expr, ast.Attribute):
        return expr.attr in EXPIRY_EXCEPTIONS
    if isinstance(expr, ast.Tuple):
        return any(map(_is_expiry_exception, expr.elts))
    return False


def _is_expiry_context(expr: ast.expr) -> bool:
    """Return whether ``expr`` is a `with` item that expects a timeout expiry."""
    if not isinstance(expr, ast.Call):
        return False
    func = expr.func
    if isinstance(func, ast.Attribute):
        name = func.attr
    elif isinstance(func, ast.Name):
        name = func.id
    else:
        return False
    return name in EXPIRY_CONTEXT_FUNCTIONS and any(map(_is_expiry_exception, expr.args))


def _catches_expiry(node: ast.Try | ast.TryStar) -> bool:
    """Return whether any handler of ``node`` catches a timeout expiry."""
    return any(
        handler.type is not None and _is_expiry_exception(handler.type) for handler in node.handlers
    )


def _is_mock_module(module: str) -> bool:
    return any(module == root or module.startswith(f"{root}.") for root in MOCK_MODULES)


def _is_monkeypatch_factory(expr: ast.expr) -> bool:
    """Return whether ``expr`` builds a MonkeyPatch: `MonkeyPatch()` or `.context()`."""
    if not isinstance(expr, ast.Call):
        return False
    func = expr.func
    if isinstance(func, ast.Attribute) and func.attr == "context":
        func = func.value
    return (isinstance(func, ast.Name) and func.id == "MonkeyPatch") or (
        isinstance(func, ast.Attribute) and func.attr == "MonkeyPatch"
    )


def _root_name(node: ast.expr) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _dotted_name(node: ast.expr) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def library_packages(repo_root: Path, relative: str) -> frozenset[str]:
    """Return the package names under `src/` of the library owning ``relative``.

    Only paths under `libs/<name>/tests/` and `sdk/<name>/tests/` have an owning
    library; every other path returns an empty set.
    """
    match = LIBRARY_TESTS_RE.match(relative)
    if match is None:
        return frozenset()
    library_dir = repo_root / relative.split("/", 1)[0] / match.group(1)
    src = library_dir / "src"
    if not src.is_dir():
        return frozenset()
    return frozenset(entry.name for entry in src.iterdir() if (entry / "__init__.py").is_file())


def scan_source(source: str, relative: str, packages: frozenset[str]) -> Scan:
    """Count the sites in one module's ``source``, honoring exemption comments.

    Raises:
        ConfigError: If ``source`` is not valid Python.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ConfigError.unparsable(relative, exc) from exc
    reasons, standalone = _comment_lines(source)
    scan = Scan()
    for line, reason in sorted(reasons.items()):
        if not reason:
            scan.empty_exemptions.append((relative, line))
    exempt = {line for line, reason in reasons.items() if reason}
    for rule, line in _Sites(packages).collect(tree):
        if line in exempt or (line - 1 in exempt and line - 1 in standalone):
            continue
        key = (relative, rule)
        scan.counts[key] = scan.counts.get(key, 0) + 1
    return scan


def scan_typescript_source(source: str, relative: str) -> Scan:
    """Count ambient timer calls in one TypeScript test source."""
    tokens, reasons, standalone = _typescript_tokens(source)
    scan = Scan()
    for line, reason in sorted(reasons.items()):
        if not reason:
            scan.empty_exemptions.append((relative, line))
    exempt = {line for line, reason in reasons.items() if reason}
    for line in _typescript_wall_clock_lines(tokens):
        if line in exempt or (line - 1 in exempt and line - 1 in standalone):
            continue
        key = (relative, RULE_WALL_CLOCK_SYNC)
        scan.counts[key] = scan.counts.get(key, 0) + 1
    return scan


def _scanned_files(repo_root: Path, roots: Iterable[str]) -> list[Path]:
    files: set[Path] = set()
    for pattern in roots:
        for directory in repo_root.glob(pattern):
            if not directory.is_dir():
                continue
            candidates = (*directory.rglob("*.py"), *directory.rglob("*.test.ts"))
            for path in candidates:
                relative = path.relative_to(repo_root)
                if not SKIPPED_DIR_NAMES.intersection(relative.parts):
                    files.add(path)
    return sorted(files)


def measure(repo_root: Path, roots: Iterable[str]) -> Scan:
    """Scan every test file under ``roots`` and merge the results."""
    total = Scan()
    for path in _scanned_files(repo_root, roots):
        relative = path.relative_to(repo_root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError.unreadable(path, exc) from exc
        if relative.endswith(".test.ts"):
            scan = scan_typescript_source(source, relative)
        else:
            scan = scan_source(source, relative, library_packages(repo_root, relative))
        total.counts.update(scan.counts)
        total.empty_exemptions.extend(scan.empty_exemptions)
    return total


def compare(counts: dict[Key, int], baseline: dict[Key, int]) -> Comparison:
    """Compare current counts with the baseline."""
    failures: list[str] = []
    for (path, rule), count in sorted(counts.items()):
        recorded = baseline.get((path, rule))
        if recorded is None:
            failures.append(f"  {path}: {rule} x{count}, not in baseline")
        elif count > recorded:
            failures.append(f"  {path}: {rule} x{count} > {recorded} (baseline)")
    stale: list[str] = []
    tightenable: list[str] = []
    for (path, rule), recorded in sorted(baseline.items()):
        count = counts.get((path, rule), 0)
        if count == 0:
            stale.append(f"  {path}: {rule}: count is 0 or file is gone")
        elif count < recorded:
            tightenable.append(f"  {path}: {rule}: {recorded} -> {count}")
    return Comparison(failures, stale, tightenable)


def _empty_exemption_lines(scan: Scan) -> list[str]:
    return [
        f"  {path}:{line}: "
        f"`{'// test-isolation:' if path.endswith('.test.ts') else '# test-isolation:'}` "
        "needs a reason"
        for path, line in sorted(scan.empty_exemptions)
    ]


def report(scan: Scan, comparison: Comparison) -> int:
    """Print the outcome and return the process exit code."""
    empty = _empty_exemption_lines(scan)
    sections: list[tuple[str, list[str]]] = [
        ("Test isolation sites over their baseline:", comparison.failures),
        ("Empty exemptions:", empty),
        ("Stale isolation baseline entries; delete them (--write):", comparison.stale),
    ]
    printed = False
    for title, lines in sections:
        if not lines:
            continue
        if printed:
            print()
        print(title)
        for line in lines:
            print(line)
        printed = True
    if comparison.failures or empty:
        print(f"\n{REMEDIATION}")
    if printed:
        return EXIT_VIOLATIONS

    print("Test isolation is within the recorded baseline.")
    if comparison.tightenable:
        print("\nBaseline entries that shrank; lower the recorded count (--write):")
        for line in comparison.tightenable:
            print(line)
    return EXIT_OK


def write_baseline(scan: Scan, baseline_path: Path) -> int:
    """Regenerate the baseline, refusing to grow it once it exists."""
    empty = _empty_exemption_lines(scan)
    if empty:
        print("Fix empty exemptions before writing the baseline:")
        for line in empty:
            print(line)
        return EXIT_VIOLATIONS
    if baseline_path.exists():
        recorded = load_baseline(baseline_path)
        grown = [
            f"  {path}: {rule} x{count} > {recorded.get((path, rule), 0)} (baseline)"
            for (path, rule), count in sorted(scan.counts.items())
            if count > recorded.get((path, rule), 0)
        ]
        if grown:
            print("Refusing to write: the baseline may only shrink.")
            for line in grown:
                print(line)
            print(f"\n{REMEDIATION}")
            return EXIT_VIOLATIONS
    baseline_path.write_text(format_baseline(scan.counts), encoding="utf-8")
    entries = sum(1 for count in scan.counts.values() if count > 0)
    print(f"Wrote {entries} entries to {baseline_path}.")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Enforce the test isolation ratchet, returning a process exit code."""
    parser = argparse.ArgumentParser(description="Enforce the test isolation ratchet.")
    parser.add_argument(
        "--root", type=Path, default=Path(), help="Repository root to scan (default: cwd)"
    )
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=None,
        help="pyproject.toml holding the configuration (default: <root>/pyproject.toml)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Regenerate the baseline; only allowed to shrink it or to create it",
    )
    args = parser.parse_args(argv)
    pyproject_path = args.pyproject if args.pyproject is not None else args.root / DEFAULT_PYPROJECT

    try:
        config = load_config(pyproject_path)
        scan = measure(args.root, config.roots)
        baseline_path = args.root / config.baseline
        if args.write:
            return write_baseline(scan, baseline_path)
        comparison = compare(scan.counts, load_baseline(baseline_path))
    except (ConfigError, OSError) as exc:
        print(f"check_test_isolation: {exc}", file=sys.stderr)
        return EXIT_TOOL_ERROR
    return report(scan, comparison)


if __name__ == "__main__":
    raise SystemExit(main())
