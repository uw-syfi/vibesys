"""Real time, threads, processes, sockets and signals stay behind ``vs_sim`` (roadmap #1636, #1639).

Two tiers exist. The deterministic tiers (unit and composition tests: every test
directory except ``sim_real_tiers`` in ``pyproject.toml``) run on the virtual loop and
Fakes, so a test's result never depends on timing. The real tiers (``tests/e2e``,
``tests/slurm_cluster``, ``tests/minimal_container``) drive real systems and may use
anything. Product code reaches the real clock, threads, processes, sockets and signals
only through the ``vs_sim.api`` interfaces, whose real implementations live in
``libs/vs-sim``.

This check counts, per file and rule, each use of the real APIs below. Names are resolved
through the file's import table (``import x as y``, ``from x import y``, ``from x import *``),
so ``from time import monotonic`` and ``import subprocess as sp`` count like the plain
spelling, and a dotted chain is counted once. ``getattr(x, "name")`` and
``importlib.import_module("x")`` / ``__import__("x")`` with a literal count as the name they
reach.

* ``time``, ``threading``, ``subprocess``, ``socket``, ``signal`` (every reference to the
  module or to a name imported from it);
* ``process``: anything that starts or reaps a process or interpreter: ``multiprocessing``,
  ``pty``, ``os.fork``/``exec*``/``spawn*``/``posix_spawn*``/``system``/``popen``/``wait*``/
  ``kill``/``killpg``/``pipe*``/``openpty``, ``asyncio.create_subprocess_*``, the loop's
  ``subprocess_exec``/``subprocess_shell``/``connect_*_pipe``, and ``sys.executable``. A
  ``[sys.executable, "-m", ...]`` command line handed to a runner launches a real interpreter
  even though no ``subprocess`` name appears, so naming the interpreter is the counted act;
* ``executor``: ``concurrent.futures`` executors, ``asyncio.to_thread``, ``run_in_executor``;
* ``socket_io``: network and descriptor multiplexing outside ``socket``: ``select``,
  ``selectors``, ``ssl``, ``socketserver``, ``http.client``, ``http.server``,
  ``urllib.request``, ``websockets``, ``httpx``, ``requests``, ``aiohttp``, the
  ``asyncio.open_connection``/``start_server`` family and the loop's ``create_connection``/
  ``create_server``/``sock_*``; ``signal`` also counts the loop's ``add_signal_handler``;
* ``fs_sync``: ``os.fsync``/``fdatasync``/``sync`` and ``fcntl`` (locks);
* ``wall_clock``: ``datetime.now``/``utcnow``/``today`` and ``date.today``;
* ``asyncio.sleep`` with an argument other than literal ``0``;
* tests only: a bare ``.wait()``, ``.join()``, ``.get()`` or ``.communicate()`` with no
  argument (not awaited, and not handed to ``create_task``-style wrappers, which build a
  coroutine and park nothing), and a signal sent to the test's own process
  (``os.kill(os.getpid(), ...)``, ``signal.raise_signal``).

Not seen: a name built at run time (``getattr(os, name)``), ``from os import *``, an interpreter named by a
bare string such as ``"python3"``, and ``eval``/``exec``.

Product code is ``src/`` and ``libs/*/src``; ``libs/vs-sim`` is exempt as the owner of the
real adapters. Existing uses are recorded with exact counts in
``real_api_baseline.jsonl``. The check fails when a count grows, when a new file appears,
and when a count shrinks without the baseline being lowered, so the list only gets
shorter. To shrink it, move the site onto a ``vs_sim`` interface or Fake and lower the
count in the baseline.
"""

import ast
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

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
    **_names("time", "time"),
    **_names("threading", "threading"),
    **_names("subprocess", "subprocess"),
    **_names("socket", "socket"),
    **_names("signal", "signal"),
    **_names(
        "process",
        "multiprocessing",
        "pty",
        "sys.executable",
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
    **_names(
        "wall_clock",
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "datetime.datetime.today",
        "datetime.date.today",
    ),
}
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
PRODUCT_RULES = (*sorted(set(REAL_NAMES.values()) | set(REAL_METHODS.values())), "asyncio_sleep")
PRODUCT_RULES = (*sorted(set(REAL_NAMES.values()) | set(REAL_METHODS.values())), "asyncio_sleep")
TEST_RULES = (*PRODUCT_RULES, "bare_wait", "self_signal")
WAIT_ATTRS = frozenset({"wait", "join", "get", "communicate"})
# Calls whose coroutine argument runs on the loop, where it is cancellable.
COROUTINE_WRAPPERS = frozenset(
    {"create_task", "ensure_future", "gather", "wait_for", "shield", "arrival"}
)
SIGNAL_SENDERS = frozenset({"os.kill", "os.killpg", "signal.raise_signal", "signal.pthread_kill"})

Key = tuple[str, str]

_SHARED_ROOTS = ("os", "sys", "asyncio", "datetime")
_OWN_ROOTS = sorted({key.split(".")[0] for key in REAL_NAMES} - set(_SHARED_ROOTS))
_LEAVES = sorted({key.split(".")[-1] for key in REAL_NAMES if key.split(".")[0] in _SHARED_ROOTS})
# A cheap superset of the sources `violations` can count, so most files are never parsed: an
# import of a root only a real API uses, any use of a name inside a root the repository shares
# with harmless code, a method name that reaches a real facility, or a dynamic spelling.
_MAYBE = re.compile(
    rf"\b(?:import|from)\b[^\n]*\b(?:{'|'.join(_OWN_ROOTS)})\b"
    rf"|\b(?:{'|'.join(_SHARED_ROOTS)})\.(?:{'|'.join(_LEAVES)}|date\b)"
    rf"|\bfrom (?:{'|'.join(_SHARED_ROOTS)})\b|\bimport (?:{'|'.join(_SHARED_ROOTS)})\s+as\b"
    rf"|\.(?:{'|'.join(REAL_METHODS)})\b|\bfrom\s+\S+\s+import\s+\*"
    r"|\bsleep\b|raise_signal|pthread_kill|getpid|import_module|__import__"
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


def _is_zero(expr: ast.expr) -> bool:
    return isinstance(expr, ast.Constant) and expr.value == 0 and not isinstance(expr.value, bool)


def _nonzero_sleep(call: ast.Call, aliases: Mapping[str, str]) -> bool:
    if _dotted(call.func, aliases) != "asyncio.sleep":
        return False
    argument = call.args[0] if call.args else next((k.value for k in call.keywords), None)
    return argument is None or not _is_zero(argument)


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
    nodes = list(ast.walk(ast.parse(source)))
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
            found["asyncio_sleep"] += _nonzero_sleep(node, aliases)
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
    "time.sleep": ("import time\n\ndef test_a():\n    time.sleep(1)\n", "time"),
    "time-alias": ("import time as t\n\ndef test_a():\n    t.monotonic()\n", "time"),
    "from-time": ("from time import sleep\n\ndef test_a():\n    sleep(1)\n", "time"),
    "threading": ("import threading\n\ndef test_a():\n    threading.Event()\n", "threading"),
    "subprocess": ("import subprocess\n\ndef test_a():\n    subprocess.run(['x'])\n", "subprocess"),
    "socket": ("import socket\n\ndef test_a():\n    socket.socket()\n", "socket"),
    "signal": ("import signal\n\ndef test_a():\n    signal.SIGTERM\n", "signal"),
    "asyncio.sleep": (
        "import asyncio\n\nasync def test_a():\n    await asyncio.sleep(5)\n",
        "asyncio_sleep",
    ),
    "asyncio-from": (
        "from asyncio import sleep\n\nasync def test_a():\n    await sleep(0.1)\n",
        "asyncio_sleep",
    ),
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
        "process",
    ),
    "from sys": (
        "from sys import executable\n\ndef test_a():\n    cmd = [executable, '-c', '1']\n",
        "process",
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
    "datetime now": (
        "from datetime import datetime\n\ndef test_a():\n    datetime.now()\n",
        "wall_clock",
    ),
    "datetime alias": (
        "import datetime as dt\n\ndef test_a():\n    dt.datetime.utcnow()\n",
        "wall_clock",
    ),
    "date today": (
        "from datetime import date\n\ndef test_a():\n    date.today()\n",
        "wall_clock",
    ),
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
}
CLEAN = {
    "zero sleep": "import asyncio\n\nasync def test_a():\n    await asyncio.sleep(0)\n",
    "awaited wait": "async def test_a(event):\n    await event.wait()\n",
    "bounded wait": "def test_a(event, t):\n    event.wait(timeout=1)\n    t.join(1)\n",
    "str join": "def test_a(parts):\n    ','.join(parts)\n    parts.get('k')\n",
    "coroutine handed to the loop": (
        "import asyncio\n\nasync def test_a(q):\n    task = asyncio.create_task(q.get())\n"
    ),
    "datetime arithmetic": (
        "from datetime import UTC, datetime\n\ndef test_a():\n    datetime(2020, 1, 1, tzinfo=UTC)\n"
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
    "time": "time",
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
    assert violations(PLANTED["time.sleep"][0], PRODUCT_RULES) == Counter(time=1)


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
        "a deterministic-tier test must not depend on real time, threads, processes, sockets "
        "or signals: use vs_sim.api.testing (VirtualClock, Gate, Fakes) instead, or move a "
        "test of a real system into a real tier (sim_real_tiers).\n" + "\n".join(problems)
    )


def test_product_code_reaches_real_apis_only_through_vs_sim() -> None:
    found = scan(_python_files(PRODUCT_ROOTS), PRODUCT_RULES, ())
    problems = ratchet_problems(found, _baseline(product=True))
    assert not problems, (
        "product code takes the vs_sim.api interfaces (Clock, Sleeper, BlockingRunner, "
        "SignalSource, ProcessLauncher) instead of using time, threading, subprocess, "
        "socket or signal directly.\n" + "\n".join(problems)
    )
