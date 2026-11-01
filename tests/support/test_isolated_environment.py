"""Exercise pytest's environment isolation through its subprocess API."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from tests.support import run_test_command, sharding
from tests.support.child_pytest import BARE_PYTEST, child_pythonpath, root_conftest_plugins

pytest_plugins = ["pytester"]

_VARIABLES = (
    "HOME",
    "VIBESYS_STATE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    "XDG_RUNTIME_DIR",
)


_DURATIONS = {"test_fast.py": 1.0, "test_slow.py": 100.0}
_SLOW_FIRST = ["test_slow.py::test_slow", "test_fast.py::test_fast"]
_COLLECTED = ["test_fast.py::test_fast", "test_slow.py::test_slow"]


@dataclass
class _ControllerNode:
    """What the sharding plugin reads of xdist's worker controller, and what it hands the worker."""

    config: pytest.Config
    workerinput: dict[str, object] = field(default_factory=dict)


def test_the_root_conftest_loads_every_support_plugin() -> None:
    """The child runs below load one plugin each, which is the suite's wiring only if declared."""
    declared = set(root_conftest_plugins(Path(__file__).parents[2]))

    assert {
        "tests.support.isolated_environment",
        "tests.support.cwd_hygiene",
        "tests.support.tree_hygiene",
        "tests.support.path_hygiene",
        "tests.support.sharding",
    } <= declared


def _sharding_config(pytester: pytest.Pytester, *arguments: str) -> pytest.Config:
    (pytester.path / "durations.json").write_text(json.dumps(_DURATIONS))
    return pytester.parseconfig(
        "-p",
        "no:tach",
        "-p",
        "tests.support.sharding",
        "--shard-durations",
        str(pytester.path / "durations.json"),
        *arguments,
    )


@pytest.mark.parametrize(
    ("distribution", "expected"),
    [("load", _SLOW_FIRST), ("loadgroup", _SLOW_FIRST), ("loadscope", _COLLECTED)],
)
def test_duration_order_respects_the_distribution_mode(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    distribution: str,
    expected: list[str],
) -> None:
    """The controller forwards duration order to the dynamic schedulers; the worker applies what it got."""
    monkeypatch.delenv("VIBESYS_TEST_SHARD", raising=False)
    pytester.makepyfile(test_fast="def test_fast(): pass", test_slow="def test_slow(): pass")
    items, _ = pytester.inline_genitems("-p", "no:tach")
    controller = _ControllerNode(_sharding_config(pytester, "-n", "2", "--dist", distribution))
    sharding.pytest_configure_node(controller)  # ty: ignore[invalid-argument-type]
    worker = _sharding_config(pytester)
    worker.workerinput = controller.workerinput  # ty: ignore[unresolved-attribute]

    sharding.pytest_collection_modifyitems(worker, items)

    assert [item.nodeid for item in items] == expected


def test_a_run_without_workers_keeps_collection_order(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VIBESYS_TEST_SHARD", raising=False)
    pytester.makepyfile(test_fast="def test_fast(): pass", test_slow="def test_slow(): pass")
    items, _ = pytester.inline_genitems("-p", "no:tach")

    sharding.pytest_collection_modifyitems(_sharding_config(pytester), items)

    assert [item.nodeid for item in items] == _COLLECTED


def test_duration_order_reaches_real_xdist_workers(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one real-process case: ``-n 2 --dist load`` workers collect the slow file first."""
    repository = Path(__file__).parents[2]
    monkeypatch.setenv("PYTHONPATH", child_pythonpath(repository))
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    monkeypatch.delenv("VIBESYS_TEST_SHARD", raising=False)
    pytester.makeconftest(
        """
import json
import os
from pathlib import Path

def pytest_collection_finish(session):
    Path(f"collection-{os.getpid()}.json").write_text(
        json.dumps([item.nodeid for item in session.items])
    )
"""
    )
    pytester.makepyfile(test_fast="def test_fast(): pass", test_slow="def test_slow(): pass")
    durations = pytester.path / "durations.json"
    durations.write_text(json.dumps(_DURATIONS))

    result = pytester.runpytest_subprocess(
        *BARE_PYTEST,
        "-p",
        "tests.support.sharding",
        "-n",
        "2",
        "--dist",
        "load",
        "--maxschedchunk=1",
        "--shard-durations",
        str(durations),
    )

    result.assert_outcomes(passed=2)
    collections = [json.loads(path.read_text()) for path in pytester.path.glob("collection-*.json")]
    assert len(collections) == 2
    assert all(items == _SLOW_FIRST for items in collections)


def test_collection_workers_and_children_have_private_state(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Incoming real/shared homes never receive writes, even during collection."""
    repository = Path(__file__).parents[2]
    shared = pytester.path / "operator-state"
    shared.mkdir()
    sentinel = shared / "sentinel"
    sentinel.write_text("unchanged", encoding="utf-8")
    for variable in _VARIABLES:
        monkeypatch.setenv(variable, str(shared))
    for variable in ("CARGO_HOME", "RUSTUP_HOME", "GOCACHE", "GOMODCACHE"):
        monkeypatch.setenv(variable, str(shared / variable))
    monkeypatch.setenv("PYTHONPATH", child_pythonpath(repository))
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    # The nested suite checks both workers even when its parent is a CI shard.
    monkeypatch.delenv("VIBESYS_TEST_SHARD", raising=False)
    pytester.makepyfile(
        test_environment="""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

VARIABLES = (
    "HOME", "VIBESYS_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
    "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
)
# Imports can cache these paths; isolation must precede collection.
COLLECTED = {key: os.environ[key] for key in VARIABLES}
for key, value in COLLECTED.items():
    directory = Path(value)
    assert directory.is_dir()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    (directory / "collected").write_text(key)
Path(__file__).with_name(f"collection-{os.getpid()}.json").write_text(json.dumps(COLLECTED))

def test_inherited_environment(tmp_path):
    child = subprocess.run(
        [sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))"],
        check=True, capture_output=True, text=True,
    )
    inherited = json.loads(child.stdout)
    assert all(inherited[key] == os.environ[key] for key in VARIABLES)
    for key in ("CARGO_HOME", "RUSTUP_HOME", "GOCACHE", "GOMODCACHE"):
        expected = Path(__file__).parent / "operator-state" / key
        assert Path(inherited[key]) == expected
    assert Path(os.environ["VIBESYS_STATE_HOME"]).parent == tmp_path.parent
    assert os.environ["VIBESYS_STATE_HOME"] != COLLECTED["VIBESYS_STATE_HOME"]
    assert all(os.environ[key] == COLLECTED[key] for key in VARIABLES if key != "VIBESYS_STATE_HOME")
    Path(__file__).with_name("state-inherited.json").write_text(json.dumps(os.environ["VIBESYS_STATE_HOME"]))

def test_another_item(tmp_path_factory):
    state = Path(os.environ["VIBESYS_STATE_HOME"])
    base = tmp_path_factory.getbasetemp()
    assert state.parent == base
    assert not state.exists()
    assert not any(path.name.startswith("test_another_item") for path in base.iterdir())
    Path(__file__).with_name("state-pure.json").write_text(json.dumps(str(state)))
"""
    )

    result = pytester.runpytest_subprocess(
        *BARE_PYTEST,
        "-p",
        "tests.support.isolated_environment",
        "-n",
        "2",
        "-q",
        "--basetemp",
        str(pytester.path / "tmp"),
    )
    result.assert_outcomes(passed=2)

    collected = [json.loads(path.read_text()) for path in pytester.path.glob("collection-*.json")]
    assert len(collected) == 2
    states = [json.loads(path.read_text()) for path in pytester.path.glob("state-*.json")]
    assert len(states) == len(set(states)) == 2
    for variable in _VARIABLES:
        values = [record[variable] for record in collected]
        assert len(set(values)) == len(values)
        assert all(not Path(value).exists() for value in values)
    assert list(shared.iterdir()) == [sentinel]
    assert sentinel.read_text(encoding="utf-8") == "unchanged"


@pytest.mark.parametrize("private_home", [False, True], ids=["suite-home", "test-home"])
def test_git_a_test_starts_runs_no_background_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, private_home: bool
) -> None:
    """A test's commit starts no maintenance that could prune its next worktree mid-add.

    Detached maintenance after ``git commit`` deleted ``.git/worktrees/<name>``
    while a test's ``git worktree add`` was still creating it.
    """
    if private_home:
        (tmp_path / "home").mkdir()
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
    trace = tmp_path / "git-trace2.json"
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
    repository = tmp_path / "repository"
    run_test_command(["git", "init", "-q", str(repository)], check=True)
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    for message in ("first", "second"):
        run_test_command(
            ["git", *identity, "commit", "-q", "--allow-empty", "-m", message],
            cwd=repository,
            check=True,
        )
    run_test_command(
        ["git", "worktree", "add", "-q", "--detach", str(tmp_path / "worktree")],
        cwd=repository,
        check=True,
    )

    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    children = [event["argv"] for event in events if event.get("event") == "child_start"]
    assert [argv for argv in children if {"maintenance", "gc"} & set(argv)] == []
