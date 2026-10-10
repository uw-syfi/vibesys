"""Real time, threads, processes, sockets and signals stay behind ``vs_sim`` (roadmap #1636, #1639).

Two tiers exist. The deterministic tiers (unit and composition tests: every test
directory except ``sim_real_tiers`` in ``pyproject.toml``) run on the virtual loop and
Fakes, so a test's result never depends on timing. The real tiers (``tests/e2e``,
``tests/slurm_cluster``, ``tests/minimal_container``) drive real systems and may use
anything. Product code reaches the real clock, threads, processes, sockets and signals
only through the ``vs_sim.api`` interfaces, whose real implementations live in
``libs/vs-sim``.

This check counts, per file and rule, each use of:

* ``time``, ``threading``, ``subprocess``, ``socket``, ``signal`` (every reference to the
  module or to a name imported from it);
* ``asyncio.sleep`` with an argument other than literal ``0``;
* tests only: a bare ``.wait()``, ``.join()``, ``.get()`` or ``.communicate()`` with no
  argument (not awaited, and not handed to ``create_task``-style wrappers, which build a
  coroutine and park nothing), and a signal sent to the test's own process
  (``os.kill(os.getpid(), ...)``, ``signal.raise_signal``).

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

REAL_MODULES = frozenset({"time", "threading", "subprocess", "socket", "signal"})
PRODUCT_RULES = (*sorted(REAL_MODULES), "asyncio_sleep")
TEST_RULES = (*PRODUCT_RULES, "bare_wait", "self_signal")
WAIT_ATTRS = frozenset({"wait", "join", "get", "communicate"})
# Calls whose coroutine argument runs on the loop, where it is cancellable.
COROUTINE_WRAPPERS = frozenset(
    {"create_task", "ensure_future", "gather", "wait_for", "shield", "arrival"}
)
SIGNAL_SENDERS = frozenset({"os.kill", "os.killpg", "signal.raise_signal", "signal.pthread_kill"})

Key = tuple[str, str]

# A cheap superset of the sources `violations` can count, so most files are never parsed.
_MAYBE = re.compile(
    r"\b(?:import|from)\b[^\n]*\b(?:time|threading|subprocess|socket|signal)\b"
    r"|\bsleep\b|raise_signal|pthread_kill|getpid"
    r"|\.(?:wait|join|get|communicate)\(\s*\)"
)


def _aliases(tree: ast.AST) -> dict[str, str]:
    """Local name -> dotted name it was imported as (``import x as y``, ``from x import y``)."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
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


def _parked_calls(tree: ast.AST) -> set[int]:
    """Ids of calls that are awaited or built into a coroutine for the loop."""
    safe: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            safe.add(id(node.value))
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in COROUTINE_WRAPPERS:
                safe.update(id(arg) for arg in node.args if isinstance(arg, ast.Call))
    return safe


def _pid_names(tree: ast.AST, aliases: Mapping[str, str]) -> set[str]:
    """Names assigned from ``os.getpid()``."""
    return {
        target.id
        for node in ast.walk(tree)
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


def violations(source: str, rules: Iterable[str]) -> Counter[str]:
    """Count, per rule in *rules*, the uses of a real API in *source*."""
    if not _MAYBE.search(source):
        return Counter()
    tree = ast.parse(source)
    wanted = set(rules)
    aliases = _aliases(tree)
    safe = _parked_calls(tree)
    pids = _pid_names(tree, aliases)
    found: Counter[str] = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            root = aliases.get(node.id, "").split(".")[0]
            if root in REAL_MODULES:
                found[root] += 1
        elif isinstance(node, ast.Call):
            if _nonzero_sleep(node, aliases):
                found["asyncio_sleep"] += 1
            if _sends_to_self(node, aliases, pids):
                found["self_signal"] += 1
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in WAIT_ATTRS
                and not node.args
                and not node.keywords
                and id(node) not in safe
            ):
                found["bare_wait"] += 1
    return Counter({rule: count for rule, count in found.items() if rule in wanted})


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
}
CLEAN = {
    "zero sleep": "import asyncio\n\nasync def test_a():\n    await asyncio.sleep(0)\n",
    "awaited wait": "async def test_a(event):\n    await event.wait()\n",
    "bounded wait": "def test_a(event, t):\n    event.wait(timeout=1)\n    t.join(1)\n",
    "str join": "def test_a(parts):\n    ','.join(parts)\n    parts.get('k')\n",
    "coroutine handed to the loop": (
        "import asyncio\n\nasync def test_a(q):\n    task = asyncio.create_task(q.get())\n"
    ),
    "kill other": "import os\n\ndef test_a(pid):\n    os.kill(pid, 15)\n",
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
