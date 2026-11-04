"""A run's state is read from its run.log, through the one reader, and never from its state store.

A run on the step loop keeps its state in run.log: core's state is a fold of its records and the
search's is the last whole state with the changes after it (`vibesys.run.logged_run`). The state
store beside it holds the writer's lease and a format fence, and no record to decode. A reader that
opens the store and decodes the envelope it used to hold finds none, and fails only when a run it
does not know is the one it reads: two shard 2 tests did, after the cutover. So only the readers of
runs saved before the cutover may load or observe a store, and they are named here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from tests.support.parsed_source import STATIC_SCAN_GROUP, parse_source, python_files, walk_nodes

pytestmark = pytest.mark.xdist_group(STATIC_SCAN_GROUP)

REPO = Path(__file__).resolve().parents[2]
STORE_OWNER = REPO / "libs" / "vs-project"
"""The library that owns the store: its own accessors (`Project.stored_record`) read it."""
SCAN_ROOTS = (
    REPO / "src",
    *(
        root
        for root in sorted((REPO / "libs").glob("*/src"))
        if not root.is_relative_to(STORE_OWNER)
    ),
    REPO / "tests" / "e2e",
)
"""Product code and the real tier, which drives the product launcher and reads what it left."""

HISTORICAL_READERS = frozenset(
    {
        "libs/vs-runtime/src/vs_runtime/_core_preflight.py",
        "libs/vs-runtime/src/vs_runtime/_run_end.py",
    }
)
"""The readers of a run saved before the cutover: the resume of a legacy store, and the ending of a
log that holds no epoch. `recorded_ending` reads the log first."""

READS = frozenset({"load", "observe"})
STORE = "state_store"


def _opens_store(node: ast.AST, stores: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in stores
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == STORE
    )


def store_reads(source: str) -> list[int]:
    """Line numbers where a run's state store is loaded or observed."""
    if STORE not in source:
        return []
    tree = parse_source(source)
    nodes = list(walk_nodes(tree))
    stores = {
        target.id
        for node in nodes
        if isinstance(node, ast.Assign) and _opens_store(node.value, set())
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    return [
        node.lineno
        for node in nodes
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in READS
        and _opens_store(node.func.value, stores)
    ]


def test_the_checker_flags_a_store_loaded_or_observed() -> None:
    assert store_reads("run.state_store(run_id).load()") == [1]
    assert store_reads("Project.open(root).state_store(run_id).observe()") == [1]
    assert store_reads("store = project.state_store(run_id)\nstored = store.load()") == [2]
    assert store_reads("log = project.run_log_path_for(root, run_id)\nlog.exists()") == []
    assert store_reads("rows = project.state_store(run_id)\nrows.commit(None, env, fence, 0)") == []
    assert store_reads("cache.load()") == []


def test_no_reader_of_a_run_loads_its_state_store_but_the_historical_ones() -> None:
    offenders = [
        f"{path.relative_to(REPO)}:{line}"
        for root in SCAN_ROOTS
        for path in python_files(root)
        if path.relative_to(REPO).as_posix() not in HISTORICAL_READERS
        for line in store_reads(path.read_text(encoding="utf-8"))
    ]

    assert offenders == [], "read the run through vibesys.run.logged_run.read_logged_run"
