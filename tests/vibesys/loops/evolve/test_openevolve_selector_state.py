"""R4 regression: the OpenEvolve selector must never write population
snapshots to disk.

Before ``2d3cc8dc`` (and its predecessor, the old ``search_policy.py``
adapter fixed by ``c7b5f0a3``), each ``admit`` wrote a *new* directory under
``.vibesys/state/<run>/evolve/openevolve/snapshots/<iteration>-<uuid>/``
(``OpenEvolveSearchPolicy._save_full_state`` in that module's history), and
``c7b5f0a3`` ("fix: retain committed OpenEvolve snapshots") explicitly
stopped pruning old snapshot directories: every admitted individual left a
new, never-deleted directory of upstream ``ProgramDatabase`` files behind,
so disk usage under ``.vibesys/`` grew without bound over a run's lifetime.

The current selector (``src/vibesys/search/population/openevolve_selector.py``
+ ``OpenEvolveSelectorState`` in ``models.py``) keeps the same database as
in-memory data (``OpenEvolveSelectorState.files``) that is replaced wholesale
on every admit, embedded inside the population's own checkpoint document. No
directory is ever created for it. This test drives a real multi-generation
evolve run with the OpenEvolve selector end to end on the fake host, then
checks the concrete symptom directly: no ``openevolve``/``snapshots``
path exists anywhere under the project's ``.vibesys`` tree, and the
persisted selector state's program count tracks the admitted population
rather than accumulating stale history.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.vibesys.loops.evolve._support import (
    _default_profiler_responses,
    _evolution_state_store,
    _invoke_loop,
    _judge_response,
    _mutator_writes_callback,
    _project_dir,
)

from vs_agent.api.testing import FakeAgentClient

if TYPE_CHECKING:
    from pathlib import Path


def test_openevolve_selector_writes_no_snapshot_directories(
    tmp_path: Path,
    ref_file: str,
) -> None:
    """Drive 4 generations through the OpenEvolve selector, then assert (1)
    no ``openevolve``/``snapshots`` directory exists anywhere under the
    project's ``.vibesys`` tree, and (2) the persisted selector state never
    holds more database programs than individuals actually admitted so far
    (upstream MAP-Elites cell replacement can legitimately evict a
    lower-fitness program that lands in an already-occupied feature cell,
    so the count can be *less* than the admitted total, but it must never
    accumulate stale entries beyond it).

    On the pre-fix, file-backed adapter assertion (1) would fail outright:
    every admit wrote a brand-new ``snapshots/<iteration>-<uuid>/`` directory
    that was never pruned (``c7b5f0a3`` explicitly stopped deleting old
    snapshots), so the number of snapshot directories under ``.vibesys``
    would grow one-for-one with every generation, unbounded by the size of
    the live population.
    """
    n_generations = 4
    runner = FakeAgentClient()
    runner.on_invoke(_mutator_writes_callback(runner))
    runner.enqueue("judge", *[_judge_response("pass") for _ in range(n_generations)])
    runner.enqueue("profiler", *_default_profiler_responses(n_generations))

    result = _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        search_policy="openevolve",
        max_generations=n_generations,
        children_per_generation=1,
    )
    assert result is True

    project_dir = _project_dir(tmp_path)
    vibesys_dir = project_dir / ".vibesys"
    assert vibesys_dir.is_dir()
    offending = [
        path
        for path in vibesys_dir.rglob("*")
        if path.is_dir() and path.name in {"openevolve", "snapshots"}
    ]
    assert offending == [], (
        "OpenEvolve selector state must never be materialized as a directory "
        f"on disk, found: {offending}"
    )

    state = _evolution_state_store(project_dir).load()
    assert state is not None
    selector_state = state.population.selector_state
    assert selector_state is not None

    admitted = [individual for individual in state.population.individuals if individual.passed]
    # Bootstrap contributes one extra admit beyond the ``n_generations``
    # subsequent generations driven above; the exact count isn't the point.
    assert len(admitted) > n_generations

    program_files = [name for name in selector_state.files if name.startswith("programs/")]
    assert 0 < len(program_files) <= len(admitted), (
        "Persisted OpenEvolve state must track at most one database program "
        "per admitted individual, never a growing history of past admits",
        program_files,
        admitted,
    )
