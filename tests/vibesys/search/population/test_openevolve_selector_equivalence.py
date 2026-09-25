"""OpenEvolve selector: in-memory database backend matches the old file backend.

``openevolve_selector._load_files``/``_dump_files`` used to round-trip the
upstream ``ProgramDatabase`` through a ``tempfile.TemporaryDirectory`` on
every call (``ProgramDatabase.save``/``load`` are disk-only). The current
module builds/restores the database directly from ``OpenEvolveSelectorState``
in memory instead (see that module's docstring). This test captures the old
disk-based implementation as a reference and checks, via a hypothesis
property, that swapping it back in for the in-memory one never changes a
single ``select``/``admit`` outcome across randomized populations and seeds:
upstream selection semantics are unchanged, only the plumbing that feeds
upstream its database is.
"""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.evaluators.metrics import MetricSpace
from vibesys.search.population import openevolve_selector
from vibesys.search.population.models import (
    CandidateOutcome,
    OpenEvolveSelectorConfig,
    PopulationConfig,
)
from vibesys.search.population.search import PopulationSearch

if TYPE_CHECKING:
    from collections.abc import Iterator

    from openevolve.database import ProgramDatabase

# --- Reference implementation: the old tempfile round-trip, captured here so
# the production module no longer needs to carry it. ------------------------


def _old_load_files(database: ProgramDatabase, files: dict[str, str]) -> None:
    if not files:
        return
    with tempfile.TemporaryDirectory() as raw_dir:
        directory = Path(raw_dir)
        for relative, content in files.items():
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        database.load(str(directory))


def _old_dump_files(database: ProgramDatabase, *, iteration: int) -> dict[str, str]:
    with tempfile.TemporaryDirectory() as raw_dir:
        directory = Path(raw_dir)
        database.save(str(directory), iteration=iteration)
        return {
            str(path.relative_to(directory)): path.read_text()
            for path in directory.rglob("*")
            if path.is_file()
        }


@contextmanager
def _backend(load_files: object, dump_files: object) -> Iterator[None]:
    with (
        patch.object(openevolve_selector, "_load_files", load_files),
        patch.object(openevolve_selector, "_dump_files", dump_files),
    ):
        yield


Trace = tuple[tuple[int, tuple[int, ...], int | None] | None, ...]


def _drive(config: PopulationConfig, perf_metrics: list[float]) -> Trace:
    """Run a realistic admit/select sequence, wiring each proposal's lineage
    into the next admit exactly as orchestration (``loops.evolve``) does."""
    search = PopulationSearch(config)
    state = search.initial()
    parent_id: int | None = None
    policy_parent_id: str | None = None
    target_island: int | None = None
    trace: list[tuple[int, tuple[int, ...], int | None] | None] = []
    for index, perf_metric in enumerate(perf_metrics):
        outcome = CandidateOutcome(
            passed=True,
            parent_id=parent_id,
            commit=f"c{index}",
            perf_metric=perf_metric,
            summary="s",
            code=f"code-{index}-{perf_metric!r}",
            policy_parent_id=policy_parent_id,
            target_island=target_island,
        )
        _, state = search.admit(state, outcome)
        state = search.end_generation(state)
        proposal, state = search.propose(state)
        if proposal is None:
            parent_id = None
            policy_parent_id = None
            target_island = None
            trace.append(None)
        else:
            parent_id = proposal.parent.id
            policy_parent_id = proposal.policy_parent_id
            target_island = proposal.target_island
            trace.append(
                (
                    proposal.parent.id,
                    tuple(i.id for i in proposal.inspirations),
                    proposal.target_island,
                )
            )
    return tuple(trace)


_openevolve_configs = st.builds(
    OpenEvolveSelectorConfig,
    population_size=st.integers(min_value=3, max_value=8),
    archive_size=st.integers(min_value=1, max_value=4),
    num_islands=st.integers(min_value=1, max_value=3),
    migration_interval=st.integers(min_value=1, max_value=4),
    migration_rate=st.just(0.5),
)


@given(
    seed=st.integers(min_value=0, max_value=2**31 - 1),
    openevolve_config=_openevolve_configs,
    perf_metrics=st.lists(
        st.floats(min_value=-100.0, max_value=100.0, allow_nan=False, allow_infinity=False),
        min_size=2,
        max_size=8,
    ),
)
@settings(max_examples=25, deadline=None)
def test_in_memory_backend_matches_old_file_backend(
    seed: int,
    openevolve_config: OpenEvolveSelectorConfig,
    perf_metrics: list[float],
) -> None:
    config = PopulationConfig(
        selector="openevolve",
        seed=seed,
        k_top_inspirations=1,
        k_random_inspirations=1,
        space=MetricSpace(),
        openevolve=openevolve_config,
    )

    new_load_files, new_dump_files = (
        openevolve_selector._load_files,  # noqa: SLF001
        openevolve_selector._dump_files,  # noqa: SLF001
    )

    with _backend(new_load_files, new_dump_files):
        new_trace = _drive(config, perf_metrics)
    with _backend(_old_load_files, _old_dump_files):
        old_trace = _drive(config, perf_metrics)

    assert new_trace == old_trace
