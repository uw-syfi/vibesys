"""Real threads, processes, sockets and signals stay behind ``vs_sim`` (roadmap #1636, #1639).

Two tiers exist. The deterministic tiers (unit and composition tests: every test
directory except ``sim_real_tiers`` in ``pyproject.toml``) run on the virtual loop and
Fakes, so a test's result never depends on timing. The real tiers (``tests/e2e``,
``tests/slurm_cluster``, ``tests/minimal_container``) drive real systems and may use
anything. Product code reaches the real clock, threads, processes, sockets and signals
only through the ``vs_sim.api`` interfaces, whose real implementations live in
``libs/vs-sim``.

Real time is not counted here: Ruff's ``TID251`` bans ``time``, ``datetime.now``,
``utcnow``, ``today``, ``date.today`` and ``asyncio.sleep`` outright (see
``[tool.ruff.lint.flake8-tidy-imports.banned-api]`` in ``pyproject.toml``), with the
real tiers and ``libs/vs-sim`` exempt there.

This check counts, per file and rule, each use of the real APIs below. Names are resolved
through the file's import table (``import x as y``, ``from x import y``, ``from x import *``),
so ``from threading import Event`` and ``import subprocess as sp`` count like the plain
spelling, and a dotted chain is counted once. ``getattr(x, "name")`` and
``importlib.import_module("x")`` / ``__import__("x")`` with a literal count as the name they
reach.

* ``threading``, ``subprocess``, ``socket``, ``signal`` (every reference to the
  module or to a name imported from it);
* ``process``: anything that starts or reaps a process: ``multiprocessing``,
  ``pty``, ``os.fork``/``exec*``/``spawn*``/``posix_spawn*``/``system``/``popen``/``wait*``/
  ``kill``/``killpg``/``pipe*``/``openpty``, ``asyncio.create_subprocess_*``, and the loop's
  ``subprocess_exec``/``subprocess_shell``/``connect_*_pipe``;
* ``interpreter``: ``sys.executable``. A ``[sys.executable, "-m", ...]`` command line handed
  to a runner launches a real interpreter even though no ``subprocess`` name appears, so
  naming the interpreter is the counted act;
* ``executor``: ``concurrent.futures`` executors, ``asyncio.to_thread``, ``run_in_executor``;
* ``socket_io``: network and descriptor multiplexing outside ``socket``: ``select``,
  ``selectors``, ``ssl``, ``socketserver``, ``http.client``, ``http.server``,
  ``urllib.request``, ``websockets``, ``httpx``, ``requests``, ``aiohttp``, the
  ``asyncio.open_connection``/``start_server`` family and the loop's ``create_connection``/
  ``create_server``/``sock_*``; ``signal`` also counts the loop's ``add_signal_handler``;
* ``fs_sync``: ``os.fsync``/``fdatasync``/``sync`` and ``fcntl`` (locks);
* tests only, ``real_impl``: a reference to a real implementation behind a ``vs_sim``
  interface (``OsThreads``, ``SystemClock``, ``ThreadBlockingRunner``, ``Platform.real`` and
  the rest of ``REAL_IMPLEMENTATIONS``), or to a shared helper that hands one out
  (``production_timing``, ``production_runs``, ``REAL_PROCESSES``), counted where the test
  uses it. ``tests/support`` is scanned like any test directory, so a helper cannot hide
  a real construction from the tests that call it;
* tests only: a bare ``.wait()``, ``.join()``, ``.get()`` or ``.communicate()`` with no
  argument (not awaited, and not handed to ``create_task``-style wrappers, which build a
  coroutine and park nothing), and a signal sent to the test's own process
  (``os.kill(os.getpid(), ...)``, ``signal.raise_signal``).

Not seen: a name built at run time (``getattr(os, name)``), ``from os import *``, an interpreter named by a
bare string such as ``"python3"``, and ``eval``/``exec``.

Product code is ``src/`` and ``libs/*/src``; ``libs/vs-sim`` is exempt as the owner of the
real adapters. ``REAL_API_EDGES`` exempts a rule in one product file, with its reason: a
separate program that runs where ``vs_sim`` cannot be imported, or a file that only names
the interpreter's path. A module whose only logic is the call itself is listed there rather
than wrapped to clear the scan; a module with policy over the primitive (when to kill,
escalate, reap or retry) moves onto ``vs_sim``. Existing uses are recorded with exact counts in
``real_api_baseline.jsonl``. The check fails when a count grows, when a new file appears,
and when a count shrinks without the baseline being lowered, so the list only gets
shorter. To shrink it, move the site onto a ``vs_sim`` interface or Fake and lower the
count in the baseline.

The ``file_io`` rule at the end of this file is separate and has no baseline: product code
opens, writes, copies or removes files and parses JSON or TOML only in the edge modules
listed in ``FILE_IO_EDGE_MODULES``, each with its kind and a note; everything else asks the
owner of the data (see ``test_product_code_touches_files_only_in_edge_modules``).

The ``parse`` rule after it has no baseline either: product code calls pydantic's
``validate_json`` / ``model_validate_json`` or ``pydantic_core.from_json`` only in the
modules listed in ``PARSE_EDGE_MODULES``, each with its kind and a note; untrusted JSON goes
through ``vs_decode.api`` (see ``test_product_code_parses_json_only_through_vs_decode``).
"""

import ast
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from enum import StrEnum
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.parsed_source import STATIC_SCAN_GROUP, parse_source, walk_nodes

pytestmark = pytest.mark.xdist_group(STATIC_SCAN_GROUP)

REPO = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).with_name("real_api_baseline.jsonl")
TEST_ROOTS = ("tests", "libs/*/tests", "sdk/*/tests")
PRODUCT_ROOTS = ("src", "libs/*/src")
# The owner of the real adapters; its own tests exercise them directly.
EXEMPT_PREFIXES = ("libs/vs-sim/",)
# Test data trees that hold source for tests to read, not test code.
SKIPPED_PARTS = {"__pycache__", "fixtures"}


def _names(rule: str, *names: str) -> dict[str, str]:
    return dict.fromkeys(names, rule)


_EXEC_FAMILY = tuple(
    f"{family}{variant}"
    for family in ("exec", "spawn")
    for variant in ("l", "le", "lp", "lpe", "v", "ve", "vp", "vpe")
)
# Dotted name -> rule. A chain is classified by its longest entry that is a dotted prefix of
# it, so ``subprocess`` covers ``subprocess.run`` and ``os.fork`` covers only ``os.fork``.
REAL_NAMES: dict[str, str] = {
    **_names("threading", "threading"),
    **_names("subprocess", "subprocess"),
    **_names("socket", "socket"),
    **_names("signal", "signal"),
    **_names(
        "process",
        "multiprocessing",
        "pty",
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
        *(
            f"os.{name}"
            for name in (
                *(
                    "fork",
                    "forkpty",
                    "system",
                    "popen",
                    "pipe",
                    "pipe2",
                    "openpty",
                    "kill",
                    "killpg",
                ),
                *("wait", "wait3", "wait4", "waitpid", "waitid", "posix_spawn", "posix_spawnp"),
                *_EXEC_FAMILY,
            )
        ),
    ),
    **_names("interpreter", "sys.executable"),
    **_names(
        "executor",
        "asyncio.to_thread",
        "concurrent.futures.ThreadPoolExecutor",
        "concurrent.futures.ProcessPoolExecutor",
        "concurrent.futures.thread",
        "concurrent.futures.process",
    ),
    **_names(
        "socket_io",
        "select",
        "selectors",
        "requests",
        "aiohttp",
        "urllib.request.urlopen",
        "urllib.request.urlretrieve",
        "http.client.HTTPConnection",
        "http.client.HTTPSConnection",
        "http.server.HTTPServer",
        "http.server.ThreadingHTTPServer",
        "websockets.connect",
        "websockets.serve",
        "websockets.asyncio.client.connect",
        "websockets.asyncio.server.serve",
        "websockets.sync.client.connect",
        "websockets.sync.server.serve",
        "httpx.get",
        "httpx.post",
        "httpx.put",
        "httpx.patch",
        "httpx.delete",
        "httpx.request",
        "httpx.stream",
        "httpx.Client",
        "httpx.AsyncClient",
        "asyncio.open_connection",
        "asyncio.start_server",
        "asyncio.open_unix_connection",
        "asyncio.start_unix_server",
        *(
            f"socketserver.{name}"
            for name in (
                *("TCPServer", "UDPServer", "UnixStreamServer", "UnixDatagramServer"),
                *("ThreadingTCPServer", "ThreadingUDPServer", "ThreadingUnixStreamServer"),
                *("ThreadingUnixDatagramServer", "ForkingTCPServer", "ForkingUDPServer"),
            )
        ),
    ),
    **_names(
        "fs_sync",
        "os.fsync",
        "os.fdatasync",
        "os.sync",
        "fcntl.flock",
        "fcntl.lockf",
        "fcntl.fcntl",
        "fcntl.ioctl",
    ),
}
# The real implementations behind the vs_sim interfaces, and the test helpers that hand them
# out. Product code builds these at its entrypoints; a deterministic test takes the simulators
# and Fakes, so each reference here (a construction, or a use of a shared real helper) counts
# in the test that makes it, not only in the helper module that builds it.
REAL_IMPLEMENTATIONS: dict[str, str] = _names(
    "real_impl",
    *(
        f"vs_sim.api.{name}"
        for name in (
            "OsThreads",
            "SystemClock",
            "MonotonicClock",
            "ThreadBlockingRunner",
            "UnixNetwork",
            "OsProcesses",
            "LoopSignalSource",
            "PidfdProcessSignaller",
            "SubprocessLauncher",
            "InheritedStdioLauncher",
            "SubprocessCapturedTextProcessLauncher",
            "SubprocessMergedOutputProcessLauncher",
            "SubprocessPipedProcessLauncher",
            "SubprocessProbe",
            "SubprocessStreamingCommand",
            "SystemRandomSource",
            "UrllibLoopbackHttp",
            "Platform.real",
        )
    ),
    "entrypoints.production.production_runs",
    "entrypoints.production.production_timing",
    "entrypoints.production.production_web_discovery",
    "entrypoints.server.real_gateway_effects",
    "tests.support.real_git.REAL_PROCESSES",
    "tests.support.real_git.REAL_GIT",
)
REAL_NAMES.update(REAL_IMPLEMENTATIONS)
# Method names that reach a real facility from any object (an event loop is the usual one).
REAL_METHODS: dict[str, str] = {
    **_names("executor", "run_in_executor"),
    **_names("process", "subprocess_exec", "subprocess_shell", "connect_read_pipe"),
    **_names("process", "connect_write_pipe"),
    **_names("signal", "add_signal_handler", "remove_signal_handler"),
    **_names(
        "socket_io",
        "create_connection",
        "create_server",
        "create_unix_connection",
        "create_unix_server",
        "create_datagram_endpoint",
        "sock_connect",
        "sock_accept",
        "sock_recv",
        "sock_sendall",
    ),
}
# Product code builds real implementations at its entrypoints, so only tests count them here.
PRODUCT_RULES = tuple(
    sorted((set(REAL_NAMES.values()) | set(REAL_METHODS.values())) - {"real_impl"})
)
TEST_RULES = (*PRODUCT_RULES, "real_impl", "bare_wait", "self_signal")
WAIT_ATTRS = frozenset({"wait", "join", "get", "communicate"})
# Calls whose coroutine argument runs on the loop, where it is cancellable.
COROUTINE_WRAPPERS = frozenset(
    {"create_task", "ensure_future", "gather", "wait_for", "shield", "arrival"}
)
SIGNAL_SENDERS = frozenset({"os.kill", "os.killpg", "signal.raise_signal", "signal.pthread_kill"})

Key = tuple[str, str]

_SHARED_ROOTS = ("os", "sys", "asyncio")
_OWN_ROOTS = sorted(
    {key.split(".")[0] for key in REAL_NAMES if key not in REAL_IMPLEMENTATIONS}
    - set(_SHARED_ROOTS)
)
# The implementations' roots (vs_sim, tests) are imported everywhere, so the prefilter looks
# for their names instead (``Platform`` stands for ``Platform.real``).
_IMPLEMENTATION_NAMES = sorted(
    {key.split(".")[-1] for key in REAL_IMPLEMENTATIONS} - {"real"} | {"Platform"}
)
_LEAVES = sorted({key.split(".")[-1] for key in REAL_NAMES if key.split(".")[0] in _SHARED_ROOTS})
# A cheap superset of the sources `violations` can count, so most files are never parsed: an
# import of a root only a real API uses, any use of a name inside a root the repository shares
# with harmless code, a method name that reaches a real facility, or a dynamic spelling.
_MAYBE = re.compile(
    rf"\b(?:import|from)\b[^\n]*\b(?:{'|'.join(_OWN_ROOTS)})\b"
    rf"|\b(?:{'|'.join(_SHARED_ROOTS)})\.(?:{'|'.join(_LEAVES)})"
    rf"|\bfrom (?:{'|'.join(_SHARED_ROOTS)})\b|\bimport (?:{'|'.join(_SHARED_ROOTS)})\s+as\b"
    rf"|\.(?:{'|'.join(REAL_METHODS)})\b|\bfrom\s+\S+\s+import\s+\*"
    rf"|\b(?:{'|'.join(_IMPLEMENTATION_NAMES)})\b"
    r"|raise_signal|pthread_kill|getpid|import_module|__import__"
    r"|getattr\(\s*[\w.]+\s*,\s*[\"']"
    r"|\.(?:wait|join|get|communicate)\(\s*\)"
)


def _aliases(nodes: Iterable[ast.AST]) -> dict[str, str]:
    """Local name -> dotted name it was imported as (``import x as y``, ``from x import y``)."""
    names: dict[str, str] = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    names[alias.asname] = alias.name
                else:
                    root = alias.name.split(".")[0]
                    names[root] = root
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def _dotted(expr: ast.expr, aliases: Mapping[str, str]) -> str | None:
    """The import-resolved dotted name of a ``Name`` or ``a.b.c`` chain, else ``None``."""
    parts: list[str] = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if not isinstance(expr, ast.Name) or expr.id not in aliases:
        return None
    return ".".join([aliases[expr.id], *reversed(parts)])


def _parked_calls(nodes: Iterable[ast.AST]) -> set[int]:
    """Ids of calls that are awaited or built into a coroutine for the loop."""
    safe: set[int] = set()
    for node in nodes:
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            safe.add(id(node.value))
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in COROUTINE_WRAPPERS:
                safe.update(id(arg) for arg in node.args if isinstance(arg, ast.Call))
    return safe


def _pid_names(nodes: Iterable[ast.AST], aliases: Mapping[str, str]) -> set[str]:
    """Names assigned from ``os.getpid()``."""
    return {
        target.id
        for node in nodes
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and _dotted(node.value.func, aliases) == "os.getpid"
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def _sends_to_self(call: ast.Call, aliases: Mapping[str, str], pids: set[str]) -> bool:
    target = _dotted(call.func, aliases)
    if target == "signal.raise_signal":
        return True
    if target not in SIGNAL_SENDERS or not call.args:
        return False
    first = call.args[0]
    if isinstance(first, ast.Name):
        return first.id in pids
    return isinstance(first, ast.Call) and _dotted(first.func, aliases) == "os.getpid"


def _rule_of(dotted: str) -> str:
    """The rule of the longest ``REAL_NAMES`` entry that prefixes *dotted*, else ``""``."""
    parts = dotted.split(".")
    for end in range(len(parts), 0, -1):
        rule = REAL_NAMES.get(".".join(parts[:end]))
        if rule:
            return rule
    return ""


def _literal(expr: ast.expr) -> str | None:
    return expr.value if isinstance(expr, ast.Constant) and isinstance(expr.value, str) else None


def _dynamic_rule(call: ast.Call, aliases: Mapping[str, str]) -> str:
    """The rule reached by ``getattr(x, "n")``, ``import_module("x")`` or ``__import__("x")``."""
    func = call.func
    if not call.args or (literal := _literal(call.args[-1])) is None:
        return ""
    if isinstance(func, ast.Name) and func.id == "getattr" and len(call.args) >= 2:
        base = _dotted(call.args[0], aliases)
        # A real module as the base is already counted for the reference to it.
        return "" if base is None or _rule_of(base) else _rule_of(f"{base}.{literal}")
    if len(call.args) == 1 and (
        _dotted(func, aliases) == "importlib.import_module"
        or (isinstance(func, ast.Name) and func.id == "__import__")
    ):
        return _rule_of(literal)
    return ""


def _reference_rule(node: ast.AST, inner: set[int], aliases: Mapping[str, str]) -> str:
    """The rule of a name or maximal ``a.b.c`` chain (the chain's inner parts are skipped)."""
    if id(node) in inner:
        return ""
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        return _rule_of(_dotted(node, aliases) or "")
    if isinstance(node, ast.Attribute):
        return _rule_of(_dotted(node, aliases) or "")
    return ""


def _is_bare_wait(call: ast.Call, safe: set[int]) -> bool:
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr in WAIT_ATTRS
        and not call.args
        and not call.keywords
        and id(call) not in safe
    )


def violations(source: str, rules: Iterable[str]) -> Counter[str]:
    """Count, per rule in *rules*, the uses of a real API in *source*."""
    if not _MAYBE.search(source):
        return Counter()
    nodes = list(walk_nodes(parse_source(source)))
    wanted = set(rules)
    aliases = _aliases(nodes)
    safe = _parked_calls(nodes) if "bare_wait" in wanted else set()
    pids = _pid_names(nodes, aliases) if "self_signal" in wanted else set()
    inner = {id(node.value) for node in nodes if isinstance(node, ast.Attribute)}
    found: Counter[str] = Counter()
    for node in nodes:
        found[_reference_rule(node, inner, aliases)] += 1
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            found[_rule_of(node.module or "")] += 1
        elif isinstance(node, ast.Attribute) and node.attr in REAL_METHODS:
            found[REAL_METHODS[node.attr]] += 1
        elif isinstance(node, ast.Call):
            found[_dynamic_rule(node, aliases)] += 1
            found["self_signal"] += _sends_to_self(node, aliases, pids)
            found["bare_wait"] += _is_bare_wait(node, safe)
    return Counter(
        {rule: count for rule, count in found.items() if count and rule in wanted and rule}
    )


def ratchet_problems(found: Mapping[Key, int], allowed: Mapping[Key, int]) -> list[str]:
    """What is wrong with *found* against the exact-count baseline *allowed*.

    A count may not exceed its baseline (a new file or rule counts as baseline 0), and a
    baseline may not exceed its count, so a fixed site forces the baseline down.
    """
    problems: list[str] = []
    for key in sorted(found.keys() | allowed.keys()):
        have, may = found.get(key, 0), allowed.get(key, 0)
        path, rule = key
        if have > may:
            problems.append(f"{path}: {rule} used {have} times, baseline allows {may}")
        elif have < may:
            problems.append(f"{path}: {rule} used {have} times; lower the baseline from {may}")
    return problems


def in_deterministic_tier(relative: str, real_tiers: Iterable[str]) -> bool:
    """Whether the repo-relative test path is outside every real-system tier."""
    return not any(relative == tier or relative.startswith(f"{tier}/") for tier in real_tiers)


def _python_files(patterns: Iterable[str]) -> list[Path]:
    roots = sorted({root for pattern in patterns for root in REPO.glob(pattern)})
    return [
        path
        for root in roots
        for path in sorted(root.rglob("*.py"))
        if not SKIPPED_PARTS & set(path.relative_to(root).parts)
    ]


def scan(paths: Iterable[Path], rules: Iterable[str], real_tiers: Iterable[str]) -> dict[Key, int]:
    """Counts by ``(repo-relative path, rule)`` over the paths in a deterministic tier."""
    rules = tuple(rules)
    tiers = tuple(real_tiers)
    found: dict[Key, int] = {}
    for path in paths:
        relative = path.relative_to(REPO).as_posix()
        if relative.startswith(EXEMPT_PREFIXES) or not in_deterministic_tier(relative, tiers):
            continue
        for rule, count in violations(path.read_text(encoding="utf-8"), rules).items():
            found[(relative, rule)] = count
    return found


def load_baseline(path: Path) -> dict[Key, int]:
    """Read ``{"path", "rule", "count"}`` JSON lines."""
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    baseline = {(entry["path"], entry["rule"]): entry["count"] for entry in entries}
    assert len(baseline) == len(entries), "duplicate (path, rule) entry in the baseline"
    return baseline


# ---- the checker itself ---------------------------------------------------------------

PLANTED = {
    "threading": ("import threading\n\ndef test_a():\n    threading.Event()\n", "threading"),
    "subprocess": ("import subprocess\n\ndef test_a():\n    subprocess.run(['x'])\n", "subprocess"),
    "socket": ("import socket\n\ndef test_a():\n    socket.socket()\n", "socket"),
    "signal": ("import signal\n\ndef test_a():\n    signal.SIGTERM\n", "signal"),
    "bare wait": ("def test_a(event):\n    event.wait()\n", "bare_wait"),
    "bare join": ("def test_a(thread):\n    thread.join()\n", "bare_wait"),
    "bare get": ("def test_a(queue):\n    queue.get()\n", "bare_wait"),
    "bare communicate": ("def test_a(proc):\n    proc.communicate()\n", "bare_wait"),
    "kill self": ("import os\n\ndef test_a():\n    os.kill(os.getpid(), 15)\n", "self_signal"),
    "kill self via name": (
        "import os\n\ndef test_a():\n    pid = os.getpid()\n    os.kill(pid, 15)\n",
        "self_signal",
    ),
    "raise_signal": (
        "from signal import raise_signal\n\ndef test_a():\n    raise_signal(15)\n",
        "self_signal",
    ),
    "kill other": ("import os\n\ndef test_a(pid):\n    os.kill(pid, 15)\n", "process"),
    "os.fork": ("import os\n\ndef test_a():\n    os.fork()\n", "process"),
    "posix_spawn": (
        "import os\n\ndef test_a():\n    os.posix_spawn('/bin/true', ['true'], {})\n",
        "process",
    ),
    "os.system": ("import os\n\ndef test_a():\n    os.system('true')\n", "process"),
    "os alias": ("import os as o\n\ndef test_a():\n    o.popen('true')\n", "process"),
    "from os": ("from os import execv\n\ndef test_a():\n    execv('/x', [])\n", "process"),
    "subprocess_exec": (
        "import asyncio\n\nasync def test_a():\n    await asyncio.create_subprocess_exec('x')\n",
        "process",
    ),
    "subprocess_shell": (
        "from asyncio import create_subprocess_shell as run\n\n"
        "async def test_a():\n    await run('x')\n",
        "process",
    ),
    "loop subprocess": (
        "async def test_a(loop):\n    await loop.subprocess_exec(object, 'x')\n",
        "process",
    ),
    "multiprocessing": (
        "import multiprocessing\n\ndef test_a():\n    multiprocessing.Process()\n",
        "process",
    ),
    "pty": ("import pty\n\ndef test_a():\n    pty.openpty()\n", "process"),
    "interpreter command": (
        "import sys\n\ndef test_a(runner):\n"
        "    runner.run([sys.executable, '-m', 'vs_slurm.fake_connector'])\n",
        "interpreter",
    ),
    "from sys": (
        "from sys import executable\n\ndef test_a():\n    cmd = [executable, '-c', '1']\n",
        "interpreter",
    ),
    "to_thread": (
        "import asyncio\n\nasync def test_a(f):\n    await asyncio.to_thread(f)\n",
        "executor",
    ),
    "thread pool": (
        "from concurrent.futures import ThreadPoolExecutor as Pool\n\ndef test_a():\n    Pool()\n",
        "executor",
    ),
    "futures module": (
        "from concurrent import futures\n\ndef test_a():\n    futures.ProcessPoolExecutor()\n",
        "executor",
    ),
    "run_in_executor": (
        "async def test_a(loop, f):\n    await loop.run_in_executor(None, f)\n",
        "executor",
    ),
    "select": (
        "import select\n\ndef test_a(fd):\n    select.select([fd], [], [], 0)\n",
        "socket_io",
    ),
    "selectors": (
        "import selectors\n\ndef test_a():\n    selectors.DefaultSelector()\n",
        "socket_io",
    ),
    "websocket": (
        "import websockets\n\nasync def test_a():\n    websockets.connect('ws://x')\n",
        "socket_io",
    ),
    "open_connection": (
        "import asyncio\n\nasync def test_a():\n    await asyncio.open_connection('h', 1)\n",
        "socket_io",
    ),
    "loop server": (
        "async def test_a(loop):\n    await loop.create_server(object)\n",
        "socket_io",
    ),
    "http server": (
        "import http.server\n\ndef test_a():\n    http.server.HTTPServer(('', 0), object)\n",
        "socket_io",
    ),
    "flock": ("import fcntl\n\ndef test_a(fd):\n    fcntl.flock(fd, 1)\n", "fs_sync"),
    "fsync": ("import os\n\ndef test_a(fd):\n    os.fsync(fd)\n", "fs_sync"),
    "signal handler": (
        "async def test_a(loop, h):\n    loop.add_signal_handler(15, h)\n",
        "signal",
    ),
    "getattr literal": (
        "import os\n\ndef test_a():\n    getattr(os, 'fork')()\n",
        "process",
    ),
    "import_module": (
        "import importlib\n\ndef test_a():\n    importlib.import_module('subprocess')\n",
        "subprocess",
    ),
    "from import_module": (
        "from importlib import import_module\n\ndef test_a():\n    import_module('threading')\n",
        "threading",
    ),
    "__import__": ("def test_a():\n    __import__('socket')\n", "socket"),
    "real threads": (
        "from vs_sim.api import OsThreads\n\ndef test_a():\n    OsThreads()\n",
        "real_impl",
    ),
    "real platform": (
        "from vs_sim.api import Platform\n\ndef test_a():\n    Platform.real()\n",
        "real_impl",
    ),
    "production timing": (
        "from entrypoints.production import production_timing\n\n"
        "def test_a():\n    production_timing()\n",
        "real_impl",
    ),
    "shared real processes": (
        "from tests.support.real_git import REAL_PROCESSES\n\n"
        "def test_a(run):\n    run(REAL_PROCESSES)\n",
        "real_impl",
    ),
}
CLEAN = {
    "awaited wait": "async def test_a(event):\n    await event.wait()\n",
    "bounded wait": "def test_a(event, t):\n    event.wait(timeout=1)\n    t.join(1)\n",
    "str join": "def test_a(parts):\n    ','.join(parts)\n    parts.get('k')\n",
    "coroutine handed to the loop": (
        "import asyncio\n\nasync def test_a(q):\n    task = asyncio.create_task(q.get())\n"
    ),
    "os without a process": "import os\n\ndef test_a(p):\n    os.path.join(p, 'x')\n    os.getpid()\n",
    "getattr off a real name": "def test_a(o):\n    getattr(o, 'fork')\n",
    "import_module of a pure module": (
        "import importlib\n\ndef test_a():\n    importlib.import_module('json')\n"
    ),
    "in-memory httpx": (
        "import httpx\n\ndef test_a():\n    httpx.MockTransport(print)\n    httpx.Response(200)\n"
    ),
    "vs_sim": (
        "from vs_sim.api.testing import start_thread\n\ndef test_a():\n    start_thread(print)\n"
    ),
    "shadowing local": "def test_a(time):\n    time.sleep(1)\n",
    "simulated platform": (
        "from vs_sim.api import Platform\nfrom vs_sim.api.testing import Sim, SimThreads\n\n"
        "def test_a(p: Platform):\n    Sim(seed=1).threads()\n    SimThreads()\n"
    ),
}


@pytest.mark.parametrize(("source", "rule"), PLANTED.values(), ids=PLANTED.keys())
def test_a_planted_violation_is_counted(source: str, rule: str) -> None:
    assert violations(source, TEST_RULES)[rule] >= 1


@pytest.mark.parametrize("source", CLEAN.values(), ids=CLEAN.keys())
def test_allowed_code_is_not_counted(source: str) -> None:
    assert not violations(source, TEST_RULES)


@given(copies=st.integers(min_value=1, max_value=20), case=st.sampled_from(sorted(PLANTED)))
def test_every_planted_site_is_counted_once(copies: int, case: str) -> None:
    source, rule = PLANTED[case]
    start = min(i for i in (source.find("async def"), source.find("def test_a")) if i >= 0)
    header, body = source[:start], source[start:]
    repeated = header + "\n\n".join(body.replace("test_a", f"test_a{n}") for n in range(copies))
    assert violations(repeated, TEST_RULES)[rule] == copies * violations(source, TEST_RULES)[rule]


STARS = {
    "subprocess": "subprocess",
    "multiprocessing": "process",
    "select": "socket_io",
}


@pytest.mark.parametrize(("module", "rule"), STARS.items(), ids=STARS.keys())
def test_a_star_import_of_a_real_module_is_counted(module: str, rule: str) -> None:
    assert violations(f"from {module} import *\n", TEST_RULES)[rule] == 1


def _spellings(key: str) -> list[str]:
    """Every way to import and name the dotted *key*, each a module that references it once."""
    root, *rest = key.split(".")
    tail = "".join(f".{part}" for part in rest)
    spellings = [f"import {root}\nx = {key}\n", f"import {root} as zz\nx = zz{tail}\n"]
    if rest:
        parent = ".".join([root, *rest[:-1]])
        spellings += [
            f"from {parent} import {rest[-1]}\nx = {rest[-1]}\n",
            f"from {parent} import {rest[-1]} as zz\nx = zz\n",
        ]
    return spellings


@given(data=st.data())
def test_every_real_name_is_counted_once_under_every_import_spelling(data: st.DataObject) -> None:
    key = data.draw(st.sampled_from(sorted(REAL_NAMES)))
    source = data.draw(st.sampled_from(_spellings(key)))
    assert violations(source, TEST_RULES) == Counter({REAL_NAMES[key]: 1}), source


@given(attr=st.sampled_from(sorted(REAL_METHODS)))
def test_every_real_method_is_counted_whatever_object_it_is_called_on(attr: str) -> None:
    assert violations(f"def test_a(x):\n    x.{attr}()\n", TEST_RULES) == Counter(
        {REAL_METHODS[attr]: 1}
    )


def test_the_prefilter_never_hides_a_countable_source() -> None:
    sources = [source for source, _ in PLANTED.values()]
    sources += [spelling for key in REAL_NAMES for spelling in _spellings(key)]
    assert all(_MAYBE.search(source) for source in sources)


def test_product_rules_leave_out_test_only_rules() -> None:
    source, _ = PLANTED["bare wait"]
    assert not violations(source, PRODUCT_RULES)
    assert not violations(PLANTED["real threads"][0], PRODUCT_RULES)
    assert violations(PLANTED["subprocess"][0], PRODUCT_RULES) == Counter(subprocess=1)


keys = st.tuples(st.sampled_from(["a.py", "b.py", "c.py"]), st.sampled_from(TEST_RULES))
counts = st.dictionaries(keys, st.integers(min_value=1, max_value=5))


@given(allowed=counts, extra=keys, growth=st.integers(min_value=1, max_value=3))
def test_the_baseline_cannot_grow(allowed: dict[Key, int], extra: Key, growth: int) -> None:
    grown = {**allowed, extra: allowed.get(extra, 0) + growth}
    assert any("baseline allows" in problem for problem in ratchet_problems(grown, allowed))


@given(allowed=counts)
def test_a_fixed_site_forces_the_baseline_down(allowed: dict[Key, int]) -> None:
    for key, count in allowed.items():
        shrunk = {**allowed, key: count - 1}
        problems = ratchet_problems({k: v for k, v in shrunk.items() if v}, allowed)
        assert [p for p in problems if "lower the baseline" in p]


@given(allowed=counts)
def test_only_an_exact_match_passes(allowed: dict[Key, int]) -> None:
    assert ratchet_problems(dict(allowed), allowed) == []


@pytest.mark.parametrize(
    ("relative", "deterministic"),
    [
        ("tests/vibesys/test_a.py", True),
        ("libs/vs-slurm/tests/test_a.py", True),
        ("tests/e2e/test_a.py", False),
        ("tests/minimal_container/sub/test_a.py", False),
        ("tests/e2e_helpers/test_a.py", True),
    ],
)
def test_real_tiers_are_the_only_exempt_test_directories(
    relative: str, *, deterministic: bool
) -> None:
    tiers = ["tests/e2e", "tests/slurm_cluster", "tests/minimal_container"]
    assert in_deterministic_tier(relative, tiers) is deterministic


# ---- the repository -------------------------------------------------------------------


def _baseline(*, product: bool) -> dict[Key, int]:
    return {k: n for k, n in load_baseline(BASELINE).items() if _is_product(k[0]) is product}


def _is_product(relative: str) -> bool:
    return "/tests/" not in relative and not relative.startswith("tests/")


def test_deterministic_tests_use_no_real_time_threads_processes_or_sockets(
    request: pytest.FixtureRequest,
) -> None:
    tiers = request.config.getini("sim_real_tiers")
    found = scan(_python_files(TEST_ROOTS), TEST_RULES, tiers)
    problems = ratchet_problems(found, _baseline(product=False))
    assert not problems, (
        "a deterministic-tier test must not depend on real threads, processes, sockets "
        "or signals: use vs_sim.api.testing (VirtualClock, Gate, Fakes) instead, or move a "
        "test of a real system into a real tier (sim_real_tiers).\n" + "\n".join(problems)
    )


_SEPARATE_PROGRAM = "a separate program that runs where vs_sim cannot be imported"

# The product files that may use one real API, each with its reason. Keep one line per
# (file, rule).
REAL_API_EDGES: dict[Key, str] = {
    **{
        ("libs/vs-sandbox/src/vs_sandbox/host_command_client.py", rule): (
            f"{_SEPARATE_PROGRAM}: the stdlib-only client staged into the agent container"
        )
        for rule in ("signal", "socket", "threading")
    },
    ("libs/vs-project/src/vs_project/_workspace_tree.py", "fs_sync"): (
        "syncs the directory descriptor a file was renamed in; vs_project's path-based "
        "directory sync would resolve the agent's tree again"
    ),
    ("libs/vs-sandbox/src/vs_sandbox/landlock.py", "process"): (
        f"{_SEPARATE_PROGRAM}: the confinement launcher execs the confined command"
    ),
    **{
        ("libs/vs-sandbox/src/vs_sandbox/modal_evaluator.py", rule): (
            f"{_SEPARATE_PROGRAM}: the stdlib-only Modal evaluator helper; only its main "
            "builds the real effects"
        )
        for rule in ("fs_sync", "socket_io", "subprocess", "threading")
    },
    **{
        ("libs/vs-sandbox/src/vs_sandbox/skypilot_evaluator.py", rule): (
            f"{_SEPARATE_PROGRAM}: the stdlib-only SkyPilot evaluator helper"
        )
        for rule in ("fs_sync", "socket")
    },
    ("libs/vs-agent/src/vs_agent/host_resource_declarations.py", "interpreter"): (
        "reads the interpreter's path to declare it as a host resource; starts nothing"
    ),
    ("libs/vs-agent/src/vs_agent/shim_translation.py", "interpreter"): (
        "names the interpreter in a command a launcher starts; starts nothing itself"
    ),
    ("libs/vs-faults/src/vs_faults/connector.py", "interpreter"): (
        "names the interpreter in the connector command a launcher starts"
    ),
    ("libs/vs-runtime/src/vs_runtime/_run_environment.py", "interpreter"): (
        "passes the interpreter's path to the run environment's settings"
    ),
    ("libs/vs-sandbox/src/vs_sandbox/fake_docker_engine.py", "interpreter"): (
        "names the interpreter in the exec client command a launcher starts"
    ),
    ("libs/vs-sandbox/src/vs_sandbox/host_sandbox.py", "interpreter"): (
        "names the interpreter in the confined command a launcher starts"
    ),
    ("libs/vs-sandbox/src/vs_sandbox/slurm_policy.py", "interpreter"): (
        "reads the interpreter's file name to allow it in a Slurm policy; starts nothing"
    ),
    ("src/entrypoints/launcher.py", "interpreter"): (
        "entrypoint: names the interpreter in the commands it starts and hands to children"
    ),
    ("src/entrypoints/server.py", "interpreter"): (
        "entrypoint: names the interpreter in the helper command it starts"
    ),
    ("src/entrypoints/web.py", "interpreter"): (
        "entrypoint: names the interpreter in the server command it starts"
    ),
}


def test_every_real_api_edge_is_a_product_file_with_a_reason() -> None:
    for (relative, rule), reason in REAL_API_EDGES.items():
        assert _is_product(relative), relative
        assert (REPO / relative).is_file(), relative
        assert rule in PRODUCT_RULES, (relative, rule)
        assert reason.strip(), relative


def test_product_code_reaches_real_apis_only_through_vs_sim() -> None:
    scanned = scan(_python_files(PRODUCT_ROOTS), PRODUCT_RULES, ())
    stale = sorted(set(REAL_API_EDGES) - set(scanned))
    assert not stale, (
        "these files no longer use the real API they are listed for; remove them from "
        f"REAL_API_EDGES: {stale}"
    )
    listed_twice = sorted(set(REAL_API_EDGES) & set(_baseline(product=True)))
    assert not listed_twice, f"in both REAL_API_EDGES and the baseline: {listed_twice}"
    found = {key: count for key, count in scanned.items() if key not in REAL_API_EDGES}
    problems = ratchet_problems(found, _baseline(product=True))
    assert not problems, (
        "product code takes the vs_sim.api interfaces (Threads, BlockingRunner, "
        "SignalSource, ProcessLauncher) instead of using threading, subprocess, "
        "socket or signal directly.\n" + "\n".join(problems)
    )


# ---- file I/O: product code touches files only in edge modules ------------------------

FILE_IO_NAMES = frozenset(
    {
        "json.load",
        "json.loads",
        "pydantic_core.from_json",
        "tomllib.load",
        "tomllib.loads",
        "os.open",
        "io.open",
        "builtins.open",
        "tempfile",
        *(
            f"shutil.{name}"
            for name in (
                *("copy", "copy2", "copyfile", "copyfileobj", "copymode", "copystat"),
                *("copytree", "rmtree", "move", "make_archive", "unpack_archive", "chown"),
            )
        ),
    }
)
# pathlib's file methods, recognised on any receiver by pathlib's own signatures.
_TEXT_KEYWORDS = frozenset({"encoding", "errors", "newline"})
_OPEN_KEYWORDS = frozenset({"mode", "buffering", *_TEXT_KEYWORDS})
_FILE_IO_PREFILTER = re.compile(
    r"\b(?:json|pydantic_core|tomllib|tempfile|shutil|io|os|builtins)\b"
    r"|\bopen\s*\(|\.(?:read_text|write_text|read_bytes|write_bytes)\s*\("
)


class EdgeKind(StrEnum):
    """Why a product module may touch files itself: the closed set of file edges."""

    STORE = "store"  # a store implementation; its callers ask it through a Protocol
    LOG = "log"  # a log sink, or the one reader of the log it writes
    OS_ADAPTER = "os adapter"  # an interface the OS offers only as files (/proc, devices)
    AGENT_WORKSPACE = "agent workspace"  # files the agent reads or writes in its workspace
    INPUT_LOADER = "input loader"  # user inputs: task folders, configuration, manifests
    OPERATOR_CONFIG = "operator config"  # operator-owned settings files
    PACKAGED_RESOURCE = "packaged resource"  # files shipped inside the package
    STATIC_FILES = "static files"  # the web client build the server serves
    GENERATOR = "generator"  # writes a tree others read: scaffolds, schemas, build contexts
    TRANSFER = "transfer"  # moves files to or from another host or a CLI that reads files
    PROCESS_BOUNDARY = "process boundary"  # hands files to, or runs as, another process
    FAKE = "fake"  # a Fake or contract-suite scaffold whose state is real files
    DECODER = "decoder"  # vs_decode's own bounded read


type Edge = tuple[EdgeKind, str]
"""An edge module's kind, and a one-line note naming what it reads or writes."""

# The edge modules: the only product modules that open, write, copy or remove files, or
# parse JSON or TOML directly. Everything else asks the owner of the data. Keep one line
# per module: its kind, and a note naming the data.
FILE_IO_EDGE_MODULES: dict[str, Edge] = {
    "libs/vs-agent/src/vs_agent/images.py": (
        EdgeKind.GENERATOR,
        "content-addresses the build context it hands to docker build",
    ),
    "libs/vs-agent/src/vs_agent/usage_records.py": (
        EdgeKind.LOG,
        "the append-only agent usage log",
    ),
    "libs/vs-decode/src/vs_decode/_decode.py": (
        EdgeKind.DECODER,
        "the decode function's bounded file read, used by stores and edge adapters",
    ),
    "libs/vs-evaluation/src/vs_evaluation/filesystem_store.py": (
        EdgeKind.STORE,
        "the file-backed EvaluationStore",
    ),
    "libs/vs-faults/src/vs_faults/connector.py": (
        EdgeKind.PROCESS_BOUNDARY,
        "fault-injection connector process whose shared state is files",
    ),
    "libs/vs-faults/src/vs_faults/plan.py": (
        EdgeKind.PROCESS_BOUNDARY,
        "hands the fault plan file to the connector process",
    ),
    "libs/vs-issue-tracker/src/vs_issue_tracker/core.py": (
        EdgeKind.STORE,
        "the file-backed IssueTracker",
    ),
    "libs/vs-issue-tracker/src/vs_issue_tracker/progress.py": (
        EdgeKind.STORE,
        "the local progress log",
    ),
    "libs/vs-project/src/vs_project/_cli_git_repository.py": (
        EdgeKind.STORE,
        "GitRepository implementation over the git CLI (git state is files)",
    ),
    "libs/vs-project/src/vs_project/_exclude_file.py": (
        EdgeKind.STORE,
        "the repository's info/exclude file",
    ),
    "libs/vs-project/src/vs_project/_fake_git_repository.py": (
        EdgeKind.FAKE,
        "GitRepository Fake that keeps a real working tree",
    ),
    "libs/vs-project/src/vs_project/_fake_git_worktree.py": (
        EdgeKind.FAKE,
        "GitRepository Fake's working tree",
    ),
    "libs/vs-project/src/vs_project/_framework_writes.py": (
        EdgeKind.AGENT_WORKSPACE,
        "framework state files in the agent worktree",
    ),
    "libs/vs-project/src/vs_project/_git_tracker.py": (
        EdgeKind.AGENT_WORKSPACE,
        "snapshots and restores the agent worktree",
    ),
    "libs/vs-project/src/vs_project/_logger.py": (EdgeKind.LOG, "the per-run log file"),
    "libs/vs-project/src/vs_project/_pygit2_git_repository.py": (
        EdgeKind.STORE,
        "GitRepository implementation over pygit2 (git state is files)",
    ),
    "libs/vs-project/src/vs_project/_run_logs.py": (
        EdgeKind.LOG,
        "the one reader of the run log files RunLogger writes",
    ),
    "libs/vs-project/src/vs_project/_state.py": (
        EdgeKind.STORE,
        "project and run state namespaces",
    ),
    "libs/vs-project/src/vs_project/_state_io.py": (
        EdgeKind.STORE,
        "atomic, durable state document writes and reads",
    ),
    "libs/vs-project/src/vs_project/_state_store.py": (EdgeKind.STORE, "the local StateStore"),
    "libs/vs-project/src/vs_project/_workspace_tree.py": (
        EdgeKind.AGENT_WORKSPACE,
        "every host read or write of an agent-chosen path, walked by descriptor",
    ),
    "libs/vs-project/src/vs_project/git_contract_scaffold.py": (
        EdgeKind.FAKE,
        "builds working trees for the GitRepository contract suite",
    ),
    "libs/vs-prompts/src/vs_prompts/contract.py": (
        EdgeKind.PACKAGED_RESOURCE,
        "template sources for static analysis",
    ),
    "libs/vs-runtime/src/vs_runtime/_artifact_store.py": (
        EdgeKind.STORE,
        "the run's artifact store",
    ),
    "libs/vs-runtime/src/vs_runtime/_docker_evaluator_tools.py": (
        EdgeKind.OS_ADAPTER,
        "host ownership marker for a bind mount",
    ),
    "libs/vs-runtime/src/vs_runtime/_evaluator_packages.py": (
        EdgeKind.PACKAGED_RESOURCE,
        "bundled evaluator packages",
    ),
    "libs/vs-runtime/src/vs_runtime/_event_journal.py": (EdgeKind.STORE, "the event journal"),
    "libs/vs-runtime/src/vs_runtime/_host_command_bridge.py": (
        EdgeKind.AGENT_WORKSPACE,
        "stages the host command client for the agent",
    ),
    "libs/vs-runtime/src/vs_runtime/_input_project.py": (
        EdgeKind.AGENT_WORKSPACE,
        "materializes the task's input project",
    ),
    "libs/vs-runtime/src/vs_runtime/_linux_cpu_profiler.py": (
        EdgeKind.OS_ADAPTER,
        "/proc and perf output, and the profile report sink",
    ),
    "libs/vs-runtime/src/vs_runtime/_macos_cpu_profiler.py": (
        EdgeKind.OS_ADAPTER,
        "the profile report sink",
    ),
    "libs/vs-runtime/src/vs_runtime/_objective_document.py": (
        EdgeKind.AGENT_WORKSPACE,
        "writes the objective document the agent reads",
    ),
    "libs/vs-runtime/src/vs_runtime/_project_materialization.py": (
        EdgeKind.AGENT_WORKSPACE,
        "materializes the project tree",
    ),
    "libs/vs-runtime/src/vs_runtime/_receipt_store.py": (EdgeKind.STORE, "evaluation receipts"),
    "libs/vs-runtime/src/vs_runtime/_skills.py": (EdgeKind.PACKAGED_RESOURCE, "skill trees"),
    "libs/vs-runtime/src/vs_runtime/_slot_meter.py": (EdgeKind.STORE, "the slot usage meter"),
    "libs/vs-runtime/src/vs_runtime/api/operation_fixtures.py": (
        EdgeKind.FAKE,
        "the effect log file",
    ),
    "libs/vs-sandbox/src/vs_sandbox/cuda_backend.py": (
        EdgeKind.OS_ADAPTER,
        "device lease record for a GPU",
    ),
    "libs/vs-sandbox/src/vs_sandbox/device_lease.py": (EdgeKind.STORE, "device lease records"),
    "libs/vs-sandbox/src/vs_sandbox/evaluator_tools.py": (
        EdgeKind.STORE,
        "installed evaluator tools and receipts",
    ),
    "libs/vs-sandbox/src/vs_sandbox/fake_docker_engine.py": (
        EdgeKind.FAKE,
        "Docker engine Fake with real build contexts",
    ),
    "libs/vs-sandbox/src/vs_sandbox/feature_image.py": (
        EdgeKind.GENERATOR,
        "packaged devcontainer features loader and docker build context generator",
    ),
    "libs/vs-sandbox/src/vs_sandbox/gpu_monitor.py": (EdgeKind.LOG, "GPU utilization log"),
    "libs/vs-sandbox/src/vs_sandbox/host_command_client.py": (
        EdgeKind.PROCESS_BOUNDARY,
        "stdlib-only client staged into the container; cannot import pydantic",
    ),
    "libs/vs-sandbox/src/vs_sandbox/landlock.py": (
        EdgeKind.OS_ADAPTER,
        "Landlock rules take O_PATH descriptors",
    ),
    "libs/vs-sandbox/src/vs_sandbox/linked_worktree.py": (
        EdgeKind.OS_ADAPTER,
        "git worktree pointer files",
    ),
    "libs/vs-sandbox/src/vs_sandbox/modal_evaluator.py": (
        EdgeKind.STORE,
        "its own deployment lease and lock files, unbounded; and json.loads of"
        " `modal container list --json` output, unbounded until process output is capped",
    ),
    "libs/vs-sandbox/src/vs_sandbox/modal_model_setup.py": (
        EdgeKind.PROCESS_BOUNDARY,
        "runs inside a Modal function; writes the volume sentinel",
    ),
    "libs/vs-sandbox/src/vs_sandbox/skypilot_bridge.py": (
        EdgeKind.STORE,
        "the SkyPilot bridge spool; json.loads only of request lines capped at 4 KiB",
    ),
    "libs/vs-sandbox/src/vs_sandbox/skypilot_config.py": (
        EdgeKind.OPERATOR_CONFIG,
        "SkyPilot settings",
    ),
    "libs/vs-sandbox/src/vs_sandbox/skypilot_evaluator.py": (
        EdgeKind.STORE,
        "its own state files; json.loads only of bridge frames and acks capped at 1 MiB",
    ),
    "libs/vs-sandbox/src/vs_sandbox/skypilot_runner.py": (
        EdgeKind.TRANSFER,
        "the task file the sky CLI reads, and its output",
    ),
    "libs/vs-sandbox/src/vs_sandbox/slurm_executor.py": (
        EdgeKind.STORE,
        "durable Slurm evaluation records",
    ),
    "libs/vs-sandbox/src/vs_sandbox/slurm_gpu_alias.py": (
        EdgeKind.OPERATOR_CONFIG,
        "the deprecated slurm-gpu file",
    ),
    "libs/vs-sandbox/src/vs_sandbox/slurm_policy.py": (
        EdgeKind.OPERATOR_CONFIG,
        "the Slurm operator file",
    ),
    "libs/vs-sandbox/src/vs_sandbox/slurm_settings_toml.py": (
        EdgeKind.OPERATOR_CONFIG,
        "the Slurm operator file a run validated",
    ),
    "libs/vs-slurm/src/vs_slurm/cluster_store.py": (EdgeKind.STORE, "the cluster state store"),
    "libs/vs-slurm/src/vs_slurm/config.py": (EdgeKind.OPERATOR_CONFIG, "the Slurm cluster file"),
    "libs/vs-slurm/src/vs_slurm/fake_cluster.py": (
        EdgeKind.FAKE,
        "Slurm cluster Fake whose state is files",
    ),
    "libs/vs-slurm/src/vs_slurm/fake_connector.py": (
        EdgeKind.FAKE,
        "Slurm connector Fake, a process whose state is files",
    ),
    "libs/vs-slurm/src/vs_slurm/fake_shell.py": (EdgeKind.FAKE, "Slurm shell Fake over files"),
    "libs/vs-slurm/src/vs_slurm/identity.py": (
        EdgeKind.PACKAGED_RESOURCE,
        "the digest of its own source",
    ),
    "libs/vs-slurm/src/vs_slurm/remote_operations.py": (
        EdgeKind.TRANSFER,
        "file transfer adapter to the cluster",
    ),
    "libs/vs-slurm/src/vs_slurm/runner.py": (
        EdgeKind.OS_ADAPTER,
        "/proc and cgroup files, and the job script directory",
    ),
    "libs/vs-slurm/src/vs_slurm/staging.py": (EdgeKind.TRANSFER, "staging to the cluster"),
    "libs/vs-slurm/src/vs_slurm/trace_replay.py": (
        EdgeKind.FAKE,
        "replay connector over a recorded trace file",
    ),
    "src/entrypoints/cli/config.py": (EdgeKind.INPUT_LOADER, "CLI configuration files"),
    "src/entrypoints/cli/inputs.py": (
        EdgeKind.INPUT_LOADER,
        "task inputs named on the command line",
    ),
    "src/entrypoints/example_catalog.py": (EdgeKind.PACKAGED_RESOURCE, "the example catalog"),
    "src/entrypoints/examples.py": (
        EdgeKind.GENERATOR,
        "scaffolds packaged examples into a user directory",
    ),
    "src/entrypoints/launcher.py": (EdgeKind.PACKAGED_RESOURCE, "package versions"),
    "src/entrypoints/server.py": (EdgeKind.PROCESS_BOUNDARY, "log file and socket directory"),
    "src/entrypoints/web.py": (EdgeKind.STATIC_FILES, "the web client build"),
    "src/entrypoints/web_assets.py": (EdgeKind.STATIC_FILES, "the web client build manifest"),
    "src/server/api/schema.py": (EdgeKind.GENERATOR, "protocol schema files"),
    "src/server/chat/transcript.py": (EdgeKind.STORE, "chat transcripts"),
    "src/server/event_index.py": (EdgeKind.STORE, "the event index"),
    "src/server/events.py": (EdgeKind.STORE, "the run's wire event log"),
    "src/server/host_helper_os.py": (
        EdgeKind.OS_ADAPTER,
        "OS adapter and project pyproject loader for the host helper",
    ),
    "src/server/registry.py": (EdgeKind.STORE, "the server instance registry"),
    "src/server/settings.py": (EdgeKind.INPUT_LOADER, "server settings"),
    "src/server/transport/discovery.py": (
        EdgeKind.STORE,
        "instance records, and a /proc OS adapter",
    ),
    "src/server/transport/websocket.py": (EdgeKind.STATIC_FILES, "the web client"),
    "src/vibesys/config.py": (EdgeKind.INPUT_LOADER, "VibeSys configuration"),
    "src/vibesys/domains/rendering.py": (EdgeKind.PACKAGED_RESOURCE, "role templates"),
    "src/vibesys/inputs/_manifest.py": (EdgeKind.INPUT_LOADER, "the task manifest"),
    "src/vibesys/inputs/_objectives.py": (EdgeKind.INPUT_LOADER, "task objectives"),
    "src/vibesys/inputs/_synthesis.py": (EdgeKind.GENERATOR, "writes a synthesized task folder"),
    "src/vibesys/orchestration/skill_selection.py": (
        EdgeKind.PACKAGED_RESOURCE,
        "skill sidecar metadata",
    ),
    "src/vibesys/run/evaluation_socket.py": (
        EdgeKind.OS_ADAPTER,
        "owns the evaluation socket directory",
    ),
}


def _is_class_name(expr: ast.expr) -> bool:
    return isinstance(expr, ast.Name) and expr.id[:1].isupper() and not expr.id.isupper()


def _is_path_io(call: ast.Call) -> bool:
    """Whether *call* is a ``read_text``/``write_text``/``read_bytes``/``write_bytes``/``open``
    call shaped like pathlib's (``Project.open(root)`` on a class is a constructor, not I/O)."""
    func = call.func
    if not isinstance(func, ast.Attribute) or _is_class_name(func.value):
        return False
    keywords = {keyword.arg for keyword in call.keywords}
    arity = len(call.args)
    shapes = {
        "read_bytes": arity == 0 and not keywords,
        "write_bytes": arity == 1 and not keywords,
        "read_text": arity <= 2 and keywords <= _TEXT_KEYWORDS,
        "write_text": 1 <= arity <= 4 and keywords <= _TEXT_KEYWORDS,
        "open": arity <= 5
        and keywords <= _OPEN_KEYWORDS
        and (arity == 0 or _literal(call.args[0]) is not None),
    }
    return shapes.get(func.attr, False)


def _is_file_io_name(dotted: str) -> bool:
    parts = dotted.split(".")
    return any(".".join(parts[:end]) in FILE_IO_NAMES for end in range(1, len(parts) + 1))


def file_io_sites(source: str) -> list[str]:
    """``line:spelling`` of each file access or direct JSON or TOML parse in *source*.

    Counted: a reference to a name in ``FILE_IO_NAMES`` under any import spelling, a call
    of the builtin ``open``, and a pathlib-shaped ``read_text``/``write_text``/
    ``read_bytes``/``write_bytes``/``open`` call on any receiver. Not seen: a name built at
    run time (``getattr(json, name)``), ``from json import *`` and ``eval``/``exec``.
    """
    if not _FILE_IO_PREFILTER.search(source):
        return []
    nodes = list(walk_nodes(parse_source(source)))
    aliases = _aliases(nodes)
    inner = {id(node.value) for node in nodes if isinstance(node, ast.Attribute)}
    sites: list[str] = []
    for node in nodes:
        if isinstance(node, (ast.Name, ast.Attribute)) and id(node) not in inner:
            if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
                continue
            dotted = _dotted(node, aliases)
            if dotted is not None and _is_file_io_name(dotted):
                sites.append(f"{node.lineno}:{dotted}")
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "open" and "open" not in aliases:
                sites.append(f"{node.lineno}:open()")
            elif _is_path_io(node) and isinstance(func, ast.Attribute):
                sites.append(f"{node.lineno}:.{func.attr}()")
    return sites


FILE_IO_PLANTED = {
    "json.loads": "import json\n\ndef f(s):\n    return json.loads(s)\n",
    "json.load alias": "import json as j\n\ndef f(s):\n    return j.load(s)\n",
    "from json": "from json import loads\n\ndef f(s):\n    return loads(s)\n",
    "from_json": "import pydantic_core\n\ndef f(s):\n    return pydantic_core.from_json(s)\n",
    "from from_json": "from pydantic_core import from_json as fj\n\ndef f(s):\n    fj(s)\n",
    "tomllib": "import tomllib\n\ndef f(s):\n    return tomllib.loads(s)\n",
    "os.open": "import os\n\ndef f(p):\n    os.open(p, os.O_RDONLY)\n",
    "io.open": "from io import open as o\n\ndef f(p):\n    o(p)\n",
    "builtins.open": "import builtins\n\ndef f(p):\n    builtins.open(p)\n",
    "open": "def f(p):\n    with open(p) as s:\n        return s.read()\n",
    "tempfile": "import tempfile\n\ndef f():\n    return tempfile.mkdtemp()\n",
    "from tempfile": "from tempfile import NamedTemporaryFile\n\ndef f():\n    NamedTemporaryFile()\n",
    "rmtree": "import shutil\n\ndef f(p):\n    shutil.rmtree(p)\n",
    "copytree": "from shutil import copytree\n\ndef f(a, b):\n    copytree(a, b)\n",
    "read_text": "def f(path):\n    return path.read_text(encoding='utf-8')\n",
    "write_text": "def f(path):\n    path.write_text('x', encoding='utf-8')\n",
    "read_bytes": "def f(path):\n    return path.read_bytes()\n",
    "write_bytes": "def f(path):\n    path.write_bytes(b'x')\n",
    "path open": "def f(path):\n    with path.open('rb') as s:\n        return s.read()\n",
    "attribute receiver": "def f(self):\n    return self.root.joinpath('a').read_text()\n",
    "constant receiver": "def f():\n    return SOURCE_FILE.read_text()\n",
}
FILE_IO_CLEAN = {
    "json.dumps": "import json\n\ndef f(v):\n    return json.dumps(v)\n",
    "shutil.which": "import shutil\n\ndef f():\n    return shutil.which('git')\n",
    "constructor open": "def f(root):\n    return Project.open(root)\n",
    "pydantic parse": "def f(model, s):\n    return model.model_validate_json(s)\n",
    "os.path": "import os\n\ndef f(p):\n    return os.path.join(p, 'x')\n",
    "url open": "def f(browser, url):\n    browser.open(url)\n",
    "local open": "from vs_project.api import open_git_repository as open\n\ndef f(p):\n    open(p)\n",
    "read with a size": "def f(stream):\n    return stream.read(10)\n",
}


@pytest.mark.parametrize("source", FILE_IO_PLANTED.values(), ids=FILE_IO_PLANTED.keys())
def test_a_planted_file_access_is_seen(source: str) -> None:
    assert len(file_io_sites(source)) == 1, file_io_sites(source)


@pytest.mark.parametrize("source", FILE_IO_CLEAN.values(), ids=FILE_IO_CLEAN.keys())
def test_code_that_touches_no_file_is_not_flagged(source: str) -> None:
    assert file_io_sites(source) == []


@given(name=st.sampled_from(sorted(FILE_IO_NAMES)), data=st.data())
def test_every_file_io_name_is_seen_under_every_import_spelling(
    name: str, data: st.DataObject
) -> None:
    key = f"{name}.mkdtemp" if name == "tempfile" else name
    source = data.draw(st.sampled_from(_spellings(key)))
    assert len(file_io_sites(source)) == 1, source


def _check_edges(edges: Mapping[str, tuple[object, str]], kinds: type[StrEnum]) -> None:
    """Every listed module exists, is product code, and has a known kind and a one-line note."""
    for relative, (kind, note) in edges.items():
        assert (REPO / relative).is_file(), f"{relative} is listed but does not exist"
        assert _is_product(relative), f"{relative} is not product code"
        assert isinstance(kind, kinds), f"{relative}: {kind!r} is not a {kinds.__name__}"
        assert note.strip(), f"{relative} needs a note"
        assert "\n" not in note, f"{relative} needs a one-line note"


def test_every_edge_module_exists_and_says_why_it_is_an_edge() -> None:
    _check_edges(FILE_IO_EDGE_MODULES, EdgeKind)


@pytest.mark.parametrize(
    ("kinds", "kind"),
    [(EdgeKind, "store implementation"), (EdgeKind, "STORE")],
)
def test_an_edge_of_unknown_kind_is_refused(kinds: type[StrEnum], kind: str) -> None:
    with pytest.raises(AssertionError, match="is not a"):
        _check_edges({"src/vibesys/config.py": (kind, "a note")}, kinds)


def test_product_code_touches_files_only_in_edge_modules() -> None:
    """The ``file_io`` rule: most product code does not know how anything is persisted.

    Product code (``src/`` and ``libs/*/src``, ``libs/vs-sim`` exempt) does not open, read,
    write, copy or remove files, create temporary files, or parse JSON or TOML with
    ``json``, ``tomllib`` or ``pydantic_core.from_json``. It asks the owner of the data:
    a store implementation behind a role-named ``Protocol`` (a file backend is one
    implementation, so another can replace it without touching callers), the agent
    workspace files module, or a loader of user inputs or packaged resources. Untrusted
    JSON is parsed by ``vs_decode.api.decode_json`` (bounded size, depth-bounded parse,
    strict model, typed ``Unreadable`` result) or a pydantic model or ``TypeAdapter``.

    Only the modules in ``FILE_IO_EDGE_MODULES`` touch files, each with its reason: store
    implementations and log sinks, OS adapters for file-only interfaces (``/proc``,
    ``/sys``, devices), the agent workspace interface, input and packaged-resource loaders,
    static file serving and code generators. There is no baseline: a new site goes behind
    one of those owners, and an edge module that stops touching files leaves the list.
    """
    found = {
        path.relative_to(REPO).as_posix(): sites
        for path in _python_files(PRODUCT_ROOTS)
        if not path.relative_to(REPO).as_posix().startswith(EXEMPT_PREFIXES)
        and (sites := file_io_sites(path.read_text(encoding="utf-8")))
    }
    leaks = [
        f"{relative}: {' '.join(sites)}"
        for relative, sites in sorted(found.items())
        if relative not in FILE_IO_EDGE_MODULES
    ]
    stale = sorted(set(FILE_IO_EDGE_MODULES) - set(found))
    assert not leaks, (
        "product code must not touch files or parse JSON or TOML itself: ask the data's "
        "owner (a store behind a role-named Protocol, the workspace files module, or an "
        "input loader), or parse untrusted JSON with vs_decode.api.decode_json or a "
        "pydantic model.\n" + "\n".join(leaks)
    )
    assert not stale, (
        "these edge modules no longer touch files; remove them from FILE_IO_EDGE_MODULES:\n"
        + "\n".join(stale)
    )


# ---- parse: untrusted JSON enters through vs_decode --------------------------------------

PARSE_METHODS = frozenset({"validate_json", "model_validate_json"})
PARSE_NAMES = frozenset({"pydantic_core.from_json"})
_PARSE_PREFILTER = re.compile(r"validate_json|from_json")


class ParseKind(StrEnum):
    """Why a product module may parse JSON with pydantic directly: the closed set."""

    OWN_STORE = "own store"  # re-reads what this process's own store or journal wrote
    ROUND_TRIP = "round trip"  # re-validates a value this process just serialized
    FAKE = "fake"  # a Fake or test kit validating the values its test scripted
    MODEL_HOOK = "model hook"  # overrides pydantic's parse method; its callers are scanned
    DECODER = "decoder"  # vs_decode itself


# The only product modules that call pydantic's JSON parsers directly. Everything else
# decodes through ``vs_decode.api`` (bounded size, depth-bounded parse, strict target, typed
# ``Unreadable`` result). Keep one line per module: its kind, and a note naming the data.
PARSE_EDGE_MODULES: dict[str, tuple[ParseKind, str]] = {
    "libs/vs-agent/src/vs_agent/api/testing.py": (
        ParseKind.FAKE,
        "the in-memory invocation store's documents",
    ),
    "libs/vs-agent/src/vs_agent/fake_client.py": (
        ParseKind.FAKE,
        "a scripted reply value, as the response model",
    ),
    "libs/vs-core/src/vs_core/_registry.py": (
        ParseKind.OWN_STORE,
        "persisted operation wires and run envelopes, and in-memory re-validation",
    ),
    "libs/vs-core/src/vs_core/_settlement.py": (
        ParseKind.OWN_STORE,
        "a recorded turn's output: vs_agent's runner decoded it under MAX_AGENT_REPLY_BYTES"
        " (8 MiB) and vs_runtime recorded it re-serialized",
    ),
    "libs/vs-core/src/vs_core/_waitable.py": (
        ParseKind.MODEL_HOOK,
        "forwards to pydantic with the waitable registry in context",
    ),
    "libs/vs-core/src/vs_core/flows/_agents.py": (
        ParseKind.OWN_STORE,
        "a recorded turn's output: vs_agent's runner decoded it under MAX_AGENT_REPLY_BYTES"
        " (8 MiB) and vs_runtime recorded it re-serialized",
    ),
    "libs/vs-core/src/vs_core/flows/_define.py": (
        ParseKind.OWN_STORE,
        "the record a flow published into its own state",
    ),
    "libs/vs-core/src/vs_core/flows/_run.py": (
        ParseKind.OWN_STORE,
        "the flow journal's reads, snapshots and operation results",
    ),
    "libs/vs-core/src/vs_core/testing/drive.py": (
        ParseKind.FAKE,
        "the test driver's scripted turn outputs",
    ),
    "libs/vs-core/src/vs_core/testing/flows.py": (
        ParseKind.FAKE,
        "the flow test kit's state round trip",
    ),
    "libs/vs-decode/src/vs_decode/_decode.py": (
        ParseKind.DECODER,
        "the bounded, strict decode every other module goes through",
    ),
    "libs/vs-evaluation/src/vs_evaluation/testing.py": (
        ParseKind.FAKE,
        "the in-memory evaluation store's values",
    ),
    "libs/vs-runtime/src/vs_runtime/_core_record.py": (
        ParseKind.OWN_STORE,
        "core records this runtime persisted",
    ),
    "libs/vs-runtime/src/vs_runtime/_receipt_store.py": (
        ParseKind.OWN_STORE,
        "evaluation receipts this runtime persisted",
    ),
    "libs/vs-runtime/src/vs_runtime/_request_inspection.py": (
        ParseKind.OWN_STORE,
        "execution results this runtime recorded",
    ),
    "libs/vs-runtime/src/vs_runtime/_shell_kit.py": (
        ParseKind.FAKE,
        "the shell test kit's scripted agent answers",
    ),
    "libs/vs-sandbox/src/vs_sandbox/slurm_executor.py": (
        ParseKind.OWN_STORE,
        "durable Slurm evaluation records this executor wrote",
    ),
    "libs/vs-slurm/src/vs_slurm/cluster.py": (
        ParseKind.OWN_STORE,
        "operation records the cluster store wrote, and in-memory re-validation",
    ),
    "src/server/events.py": (ParseKind.OWN_STORE, "the run's event log this server appends"),
    "src/vibesys/hypothesis/rounds.py": (ParseKind.OWN_STORE, "the saved hypothesis ledger"),
    "src/vibesys/issue_queue_board.py": (ParseKind.OWN_STORE, "a recorded progress payload"),
    "src/vibesys/orchestration/evolve/population/openevolve_selector.py": (
        ParseKind.OWN_STORE,
        "the saved OpenEvolve population",
    ),
    "src/vibesys/orchestration/issue_queue/core_policy/_projection.py": (
        ParseKind.OWN_STORE,
        "recorded round metrics",
    ),
    "src/vibesys/plugin_registration.py": (
        ParseKind.ROUND_TRIP,
        "an orchestration's options dict, validated as its options model",
    ),
    "src/vibesys/run/event_journal.py": (ParseKind.OWN_STORE, "the core event journal's codec"),
}


def parse_sites(source: str) -> list[str]:
    """``line:spelling`` of each direct pydantic JSON parse in *source*.

    Counted: an attribute named ``validate_json`` or ``model_validate_json`` on any receiver
    (a ``TypeAdapter``, a model class, ``super()``), and a reference to
    ``pydantic_core.from_json`` under any import spelling. Not seen: a name built at run time
    (``getattr(model, name)``) and ``eval``/``exec``.
    """
    if not _PARSE_PREFILTER.search(source):
        return []
    nodes = list(walk_nodes(parse_source(source)))
    aliases = _aliases(nodes)
    inner = {id(node.value) for node in nodes if isinstance(node, ast.Attribute)}
    sites: list[str] = []
    for node in nodes:
        if isinstance(node, ast.Attribute) and node.attr in PARSE_METHODS:
            sites.append(f"{node.lineno}:.{node.attr}")
        elif isinstance(node, (ast.Name, ast.Attribute)) and id(node) not in inner:
            if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
                continue
            if _dotted(node, aliases) in PARSE_NAMES:
                sites.append(f"{node.lineno}:from_json")
    return sites


PARSE_PLANTED = {
    "model method": "def f(model, s):\n    return model.model_validate_json(s)\n",
    "adapter method": "def f(adapter, s):\n    return adapter.validate_json(s)\n",
    "class method": "def f(s):\n    return Config.model_validate_json(s, strict=True)\n",
    "super": "class M:\n    def g(self, s):\n        return super().model_validate_json(s)\n",
    "bound method": "def f(model):\n    parse = model.model_validate_json\n    return parse\n",
    "from_json": "import pydantic_core\n\ndef f(s):\n    return pydantic_core.from_json(s)\n",
    "from from_json": "from pydantic_core import from_json as fj\n\ndef f(s):\n    fj(s)\n",
}
PARSE_CLEAN = {
    "vs_decode": "from vs_decode.api import decode_json\n\ndef f(s, m):\n    decode_json(s, m)\n",
    "python mode": "def f(model, v):\n    return model.model_validate(v)\n",
    "dump": "def f(model):\n    return model.model_dump_json()\n",
    "a method of that name": "class M:\n    def model_validate_json(self, s):\n        return s\n",
    "local from_json": "def from_json(s):\n    return s\n\ndef f(s):\n    from_json(s)\n",
}


@pytest.mark.parametrize("source", PARSE_PLANTED.values(), ids=PARSE_PLANTED.keys())
def test_a_planted_parse_is_seen(source: str) -> None:
    assert len(parse_sites(source)) == 1, parse_sites(source)


@pytest.mark.parametrize("source", PARSE_CLEAN.values(), ids=PARSE_CLEAN.keys())
def test_code_that_parses_through_vs_decode_is_not_flagged(source: str) -> None:
    assert parse_sites(source) == []


@given(name=st.sampled_from(sorted(PARSE_NAMES)), data=st.data())
def test_every_parse_name_is_seen_under_every_import_spelling(
    name: str, data: st.DataObject
) -> None:
    source = data.draw(st.sampled_from(_spellings(name)))
    assert len(parse_sites(source)) == 1, source


def test_every_parse_edge_module_exists_and_says_why_it_may_parse() -> None:
    _check_edges(PARSE_EDGE_MODULES, ParseKind)


def test_product_code_parses_json_only_through_vs_decode() -> None:
    """The ``parse`` rule: untrusted JSON enters product code through ``vs_decode``.

    ``vs_decode.api.decode_json`` and ``read_json`` cap the input's size, parse with
    pydantic's depth-bounded parser, require a strict target, and return a typed
    ``Unreadable`` instead of raising. Product code (``src/`` and ``libs/*/src``,
    ``libs/vs-sim`` exempt) calls pydantic's ``validate_json`` / ``model_validate_json`` or
    ``pydantic_core.from_json`` itself only in the modules in ``PARSE_EDGE_MODULES``: ones
    that re-read their own store, re-validate a value they just serialized, Fakes, a model
    hook, and ``vs_decode`` itself. There is no baseline: a new parse of outside input goes
    through ``vs_decode``, and a listed module that stops parsing leaves the list.
    """
    found = {
        path.relative_to(REPO).as_posix(): sites
        for path in _python_files(PRODUCT_ROOTS)
        if not path.relative_to(REPO).as_posix().startswith(EXEMPT_PREFIXES)
        and (sites := parse_sites(path.read_text(encoding="utf-8")))
    }
    leaks = [
        f"{relative}: {' '.join(sites)}"
        for relative, sites in sorted(found.items())
        if relative not in PARSE_EDGE_MODULES
    ]
    stale = sorted(set(PARSE_EDGE_MODULES) - set(found))
    assert not leaks, (
        "parse JSON from outside this process with vs_decode.api.decode_json or read_json "
        "(bounded size and depth, strict target, typed Unreadable result):\n" + "\n".join(leaks)
    )
    assert not stale, (
        "these modules no longer parse JSON directly; remove them from PARSE_EDGE_MODULES:\n"
        + "\n".join(stale)
    )


# ---- real defaults: product code takes its system interfaces, it does not build them -----
#
# A component that falls back to a real implementation when a caller passes nothing
# (``threads or OsThreads()``, ``field(default_factory=OsThreads)``, a ``Threads | None =
# None`` parameter) runs on real threads in every test that forgets to pass one, and the
# test still passes. So outside the entrypoints, product code takes ``Threads``,
# ``Processes``, ``Clock`` and the other ``vs_sim.api`` interfaces as required arguments;
# only an entrypoint builds the real implementations. Any call that builds one counts, as does
# a no-argument call to a wrapper class that builds one on its own (``transport or
# BrokerTransport()``); the ``main`` of a module that runs as a program is an entrypoint.
# ``REAL_DEFAULT_PENDING`` lists the files not yet converted, each with the work that
# converts it. It may only shrink.

ENTRYPOINT_PREFIXES = ("src/entrypoints/",)
SYSTEM_INTERFACES = frozenset(
    {
        "BlockingRunner",
        "Clock",
        "MonotonicClock",
        "Network",
        "PipedProcessLauncher",
        "Platform",
        "ProcessLauncher",
        "Processes",
        "SignalSource",
        "Threads",
    }
)
REAL_IMPLEMENTATION_LEAVES = frozenset(
    key.split(".")[-1] for key in REAL_IMPLEMENTATIONS if not key.endswith(".real")
)

_LANE_A = "lane A (Git, Slurm, Docker and the piped launcher, #2600 and #2607)"
_LANE_B = "lane B (runtime, server and entrypoints)"
_SANDBOX_WIRING = "the vs-sandbox GPU and SkyPilot wiring: thread Threads and Network down"
REAL_DEFAULT_PENDING: dict[str, str] = {
    "libs/vs-agent/src/vs_agent/images.py": _LANE_A,
    "libs/vs-runtime/src/vs_runtime/_run_environment.py": f"{_LANE_A}: OsProcesses",
    "libs/vs-runtime/src/vs_runtime/api/slurm_evaluation_stack.py": _LANE_B,
    "libs/vs-sandbox/src/vs_sandbox/accelerator_discovery.py": _SANDBOX_WIRING,
    "libs/vs-sandbox/src/vs_sandbox/agent_gpu.py": _LANE_A,
    "libs/vs-sandbox/src/vs_sandbox/cuda_backend.py": _SANDBOX_WIRING,
    "libs/vs-sandbox/src/vs_sandbox/fake_command_runner.py": (
        "the Fake takes its Threads from the test (75 call sites)"
    ),
    "libs/vs-sandbox/src/vs_sandbox/fake_compute_backend.py": (
        "builds a FakeCommandRunner, which defaults to OsThreads (see fake_command_runner.py)"
    ),
    "libs/vs-sandbox/src/vs_sandbox/gpu_monitor.py": _SANDBOX_WIRING,
    "libs/vs-sandbox/src/vs_sandbox/host_command_broker.py": (
        f"{_LANE_B}: SlurmEnvironment has no Threads or Network to hand the broker"
    ),
    "libs/vs-sandbox/src/vs_sandbox/host_sandbox.py": _SANDBOX_WIRING,
    "libs/vs-sandbox/src/vs_sandbox/rocm_backend.py": _SANDBOX_WIRING,
    "libs/vs-sandbox/src/vs_sandbox/skypilot_bridge.py": _SANDBOX_WIRING,
    "libs/vs-sandbox/src/vs_sandbox/skypilot_runner.py": _SANDBOX_WIRING,
    "libs/vs-sandbox/src/vs_sandbox/trainium_backend.py": _SANDBOX_WIRING,
    "libs/vs-slurm/src/vs_slurm/fake_cluster.py": "the Fake takes its Clock from the test",
    "src/launch/__init__.py": f"{_LANE_A}: slurm_processes",
}


def _leaf(expr: ast.expr) -> str | None:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None


_SETUP_METHODS = frozenset({"__init__", "__post_init__"})


def _inside_program_entry(tree: ast.Module) -> set[int]:
    """Node ids inside ``def main`` of a module that runs it under ``if __name__ == "__main__"``.

    Such a module is a program of its own, so its ``main`` is an entrypoint.
    """
    guarded = any(
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "__name__"
        for node in ast.walk(tree)
    )
    if not guarded:
        return set()
    return {
        id(child)
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
        for child in ast.walk(node)
    }


def _constructions(
    root: ast.AST, builders: frozenset[str], skip: frozenset[int] | set[int] = frozenset()
) -> list[ast.expr]:
    """The expressions under *root* that build a real implementation.

    A call to a real implementation, a call with no arguments to one of the *builders* (a class
    whose fields or ``__init__`` build one on their own), or a ``default_factory`` naming either.
    """
    found: list[ast.expr] = []

    def builds(expr: ast.expr) -> bool:
        return _leaf(expr) in REAL_IMPLEMENTATION_LEAVES or (
            isinstance(expr, ast.Name) and expr.id in builders
        )

    for node in ast.walk(root):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Call):
            bare = not node.args and not node.keywords
            if _leaf(node.func) in REAL_IMPLEMENTATION_LEAVES or (
                bare and isinstance(node.func, ast.Name) and node.func.id in builders
            ):
                found.append(node)
        elif isinstance(node, ast.keyword) and node.arg == "default_factory" and builds(node.value):
            found.append(node.value)
    return found


def real_builders(sources: Iterable[str]) -> frozenset[str]:
    """Names of classes that build a real implementation when constructed without arguments.

    A class builds one in a field's ``default_factory`` or in ``__init__``/``__post_init__``.
    A class that does so through another such class is found too, so ``BrokerTransport`` (whose
    fields default to real ``Network`` and ``Threads``) is a builder, and ``transport or
    BrokerTransport()`` is a real default. Names are matched without their module, so two
    classes called alike share a verdict.
    """
    parts: dict[str, list[tuple[ast.AST, set[int]]]] = {}
    for source in sources:
        tree = ast.parse(source)
        entry = _inside_program_entry(tree)
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                setup = [
                    stmt
                    for stmt in node.body
                    if isinstance(stmt, ast.AnnAssign)
                    or (isinstance(stmt, ast.FunctionDef) and stmt.name in _SETUP_METHODS)
                ]
                parts.setdefault(node.name, []).extend((stmt, entry) for stmt in setup)
    builders: frozenset[str] = frozenset()
    while True:
        found = frozenset(
            name
            for name, bodies in parts.items()
            if any(_constructions(body, builders, skip) for body, skip in bodies)
        )
        if found == builders:
            return builders
        builders = found


def _optional_interface(annotation: ast.expr | None, default: ast.expr | None) -> bool:
    """``x: Threads | None = None``: a system interface a caller may leave out."""
    if not (isinstance(default, ast.Constant) and default.value is None):
        return False
    if not (isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr)):
        return False
    sides = (annotation.left, annotation.right)
    return any(_leaf(side) in SYSTEM_INTERFACES for side in sides)


def _parameter_defaults(arguments: ast.arguments) -> Iterable[tuple[ast.arg, ast.expr | None]]:
    positional = [*arguments.posonlyargs, *arguments.args]
    padding = [None] * (len(positional) - len(arguments.defaults))
    yield from zip(positional, [*padding, *arguments.defaults], strict=True)
    yield from zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True)


def real_defaults(source: str, builders: frozenset[str] = frozenset()) -> list[int]:
    """The lines where *source* builds a real implementation or lets a caller omit one.

    Every real construction counts, not only a fallback for a missing argument: outside an
    entrypoint, a component that builds its own ``OsThreads`` cannot be driven by a simulator
    either. *builders* are the wrappers :func:`real_builders` found.
    """
    tree = ast.parse(source)
    lines = {node.lineno for node in _constructions(tree, builders, _inside_program_entry(tree))}
    for node in ast.walk(tree):
        if isinstance(node, ast.arguments):
            lines.update(
                arg.lineno
                for arg, default in _parameter_defaults(node)
                if _optional_interface(arg.annotation, default)
            )
        elif isinstance(node, ast.ClassDef):
            # A field a caller may omit; ``self.x: Clock | None = None`` in a method is state.
            lines.update(
                stmt.lineno
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign)
                and _optional_interface(stmt.annotation, stmt.value)
            )
    return sorted(lines)


PLANTED_DEFAULTS = {
    "or_default": "def f(threads=None):\n    return threads or OsThreads()\n",
    "dotted_or_default": "def f(clock=None):\n    return clock or vs_sim.api.SystemClock()\n",
    "default_factory": "@dataclass\nclass A:\n    threads: Threads = field(default_factory=OsThreads)\n",
    "optional_param": "def f(*, threads: Threads | None = None) -> None: ...\n",
    "optional_positional": "def f(clock: None | Clock = None) -> None: ...\n",
    "optional_field": "@dataclass\nclass A:\n    processes: Processes | None = None\n",
    "built_in_place": "def f() -> None:\n    run(threads=OsThreads())\n",
    "wrapper_default": "def f(t=None):\n    return t or Transport()\n",
    "wrapper_default_factory": "class A:\n    t: Transport = field(default_factory=Transport)\n",
    "wrapper_of_wrapper": "def f(t=None):\n    return t or Outer()\n",
}
WRAPPERS = (
    "@dataclass\nclass Transport:\n    threads: Threads = field(default_factory=OsThreads)\n"
    "class Outer:\n    def __init__(self):\n        self.t = Transport()\n"
)
ALLOWED_DEFAULTS = {
    "required": "def f(*, threads: Threads) -> None: ...\n",
    "or_fake": "def f(threads=None):\n    return threads or SimThreads()\n",
    "optional_other": "def f(log: Logger | None = None) -> None: ...\n",
    "optional_without_default": "def f(threads: Threads | None) -> None: ...\n",
    "optional_state": "class A:\n    def f(self):\n        self._clock: Clock | None = None\n",
    "program_main": (
        "def main() -> None:\n    run(threads=OsThreads())\n"
        'if __name__ == "__main__":\n    main()\n'
    ),
    "wrapper_given_its_parts": "def f() -> None:\n    Transport(threads=SimThreads())\n",
}


@pytest.mark.parametrize("case", sorted(PLANTED_DEFAULTS))
def test_a_planted_real_default_is_found(case: str) -> None:
    assert len(real_defaults(PLANTED_DEFAULTS[case], real_builders([WRAPPERS]))) == 1


@pytest.mark.parametrize("case", sorted(ALLOWED_DEFAULTS))
def test_a_required_or_fake_interface_is_not_a_real_default(case: str) -> None:
    assert real_defaults(ALLOWED_DEFAULTS[case], real_builders([WRAPPERS])) == []


def test_a_wrapper_that_builds_a_real_implementation_is_a_builder() -> None:
    assert real_builders([WRAPPERS]) == {"Transport", "Outer"}


def test_a_class_that_only_stores_what_it_is_given_is_not_a_builder() -> None:
    source = "class A:\n    def __init__(self, threads):\n        self.t = threads\n"
    assert real_builders([source]) == frozenset()


def test_every_pending_real_default_is_a_product_file_with_its_owner() -> None:
    for relative, owner in REAL_DEFAULT_PENDING.items():
        assert _is_product(relative), relative
        assert not relative.startswith(ENTRYPOINT_PREFIXES), relative
        assert (REPO / relative).is_file(), relative
        assert owner.strip(), relative


def test_product_code_outside_entrypoints_has_no_real_defaults() -> None:
    sources = {
        path.relative_to(REPO).as_posix(): path.read_text(encoding="utf-8")
        for path in _python_files(PRODUCT_ROOTS)
    }
    builders = real_builders(sources.values())
    found: dict[str, list[int]] = {}
    for relative, source in sources.items():
        if relative.startswith((*EXEMPT_PREFIXES, *ENTRYPOINT_PREFIXES)):
            continue
        if lines := real_defaults(source, builders):
            found[relative] = lines
    stale = sorted(set(REAL_DEFAULT_PENDING) - set(found))
    assert not stale, (
        f"these files no longer have a real default; remove them from REAL_DEFAULT_PENDING: {stale}"
    )
    new = {
        relative: lines for relative, lines in found.items() if relative not in REAL_DEFAULT_PENDING
    }
    assert not new, (
        "product code takes its Threads, Processes, Clock and other vs_sim.api interfaces as "
        "required arguments; only an entrypoint (src/entrypoints) builds the real ones:\n"
        + "\n".join(f"{relative}: lines {lines}" for relative, lines in sorted(new.items()))
    )
