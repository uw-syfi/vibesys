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

Rules (each site counts once; `timeout_verdict` counts a line once):

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

A site is exempt when its line, or a standalone comment on the line directly
above it, carries `# test-isolation: <reason>` with a non-empty reason. An
exemption with an empty reason is itself a violation (`empty_exemption`) that
is never baselinable.

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

    roots     -- glob patterns of directories scanned for `*.py`.
                 `__pycache__` and `fixtures` directories are skipped.
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
SKIPPED_DIR_NAMES = frozenset({"fixtures", "__pycache__"})

RULE_PATCH = "patch"
RULE_MOCK = "mock"
RULE_SLEEP = "sleep"
RULE_TIMEOUT_VERDICT = "timeout_verdict"
RULE_PRIVATE_IMPORT = "private_import"
RULE_EMPTY_EXEMPTION = "empty_exemption"
BASELINE_RULES = frozenset(
    {RULE_PATCH, RULE_MOCK, RULE_SLEEP, RULE_TIMEOUT_VERDICT, RULE_PRIVATE_IMPORT}
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

REMEDIATION = (
    "Use a Fake or an injectable seam, or add a reviewed `# test-isolation: <reason>` exemption."
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
        handler.type is not None and _is_expiry_exception(handler.type)
        for handler in node.handlers
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


def _scanned_files(repo_root: Path, roots: Iterable[str]) -> list[Path]:
    files: set[Path] = set()
    for pattern in roots:
        for directory in repo_root.glob(pattern):
            if not directory.is_dir():
                continue
            for path in directory.rglob("*.py"):
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
        f"  {path}:{line}: `# test-isolation:` needs a reason"
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
