"""OpenEvolve selector: upstream MAP-Elites/island database, held as data.

Every call reconstructs OpenEvolve's ``ProgramDatabase`` from
``OpenEvolveSelectorState.files`` (a snapshot of the directory tree
``ProgramDatabase.save``/``load`` use, held as in-memory text rather than on
disk), mutates it exactly as the upstream algorithm does, then serializes it
back to ``files`` wholesale. There is no directory on disk this module owns:
the temporary directory used to call the upstream save/load functions is
created and discarded within one function call, and ``files`` always holds
the *complete current* database rather than a growing history of snapshots.
This keeps state size bounded by the database's own size limits
(``population_size``/``archive_size``), not by how many times ``admit`` has
been called over a run's lifetime.

``Program.code`` is a canonical multi-file git patch; ``Program`` metadata
points back to the durable VibeSys individual id, whose git commit remains
the source of truth for materializing a candidate on disk.
"""

from __future__ import annotations

import hashlib
import json
import random
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast

from openevolve.config import DatabaseConfig
from openevolve.database import Program, ProgramDatabase

from vibesys.search.population.models import (
    Individual,
    OpenEvolveSelectorConfig,
    OpenEvolveSelectorState,
    Proposal,
    RandomState,
    _PersistedObjective,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from vibesys.evaluators.metrics import MetricSpace

__all__ = ["admit", "objective_signature", "select"]

_INDIVIDUAL_ID = "vibesys_individual_id"
_COMMIT = "vibesys_commit"


def objective_signature(space: MetricSpace) -> tuple[_PersistedObjective, ...]:
    """Return the persistable fingerprint of a space's declared axes."""
    return tuple(
        _PersistedObjective(name=objective.name, direction=objective.direction)
        for objective in space.objectives
    )


class _SortedIterationSet(set[str]):
    """Set semantics with deterministic iteration for replaying OpenEvolve."""

    def __iter__(self):  # noqa: ANN204
        return iter(sorted(super().__iter__()))


def _build_database(config: OpenEvolveSelectorConfig, seed: int | None) -> ProgramDatabase:
    return ProgramDatabase(
        DatabaseConfig(
            db_path=None,
            in_memory=True,
            log_prompts=False,
            population_size=config.population_size,
            archive_size=config.archive_size,
            num_islands=config.num_islands,
            feature_dimensions=["complexity", "diversity"],
            migration_interval=config.migration_interval,
            migration_rate=config.migration_rate,
            random_seed=seed,
        )
    )


def _load_files(database: ProgramDatabase, files: dict[str, str]) -> None:
    if not files:
        return
    with tempfile.TemporaryDirectory() as raw_dir:
        directory = Path(raw_dir)
        for relative, content in files.items():
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        database.load(str(directory))


def _dump_files(database: ProgramDatabase, *, iteration: int) -> dict[str, str]:
    with tempfile.TemporaryDirectory() as raw_dir:
        directory = Path(raw_dir)
        database.save(str(directory), iteration=iteration)
        return {
            str(path.relative_to(directory)): path.read_text()
            for path in directory.rglob("*")
            if path.is_file()
        }


@contextmanager
def _upstream_random(rng: random.Random) -> Generator[None, None, None]:
    """Isolate OpenEvolve's module-global RNG behind our own state."""
    process_state = random.getstate()
    random.setstate(rng.getstate())
    try:
        yield
    finally:
        rng.setstate(random.getstate())
        random.setstate(process_state)


def _normalize_upstream_collections(database: ProgramDatabase) -> None:
    database.programs = dict(sorted(database.programs.items()))
    database.islands = [
        island if isinstance(island, _SortedIterationSet) else _SortedIterationSet(island)
        for island in database.islands
    ]
    if not isinstance(database.archive, _SortedIterationSet):
        database.archive = _SortedIterationSet(database.archive)


def _canonicalize_new_programs(  # noqa: C901, PLR0912  # tracked: #288
    database: ProgramDatabase, program_ids_before: set[str]
) -> None:
    """Replace upstream random IDs/timestamps with state-derived values."""
    new_programs = [
        program
        for program_id, program in database.programs.items()
        if program_id not in program_ids_before
        and (program.metadata.get("migrant") or not program_id.startswith("vibesys-"))
    ]
    replacements: dict[str, str] = {}
    for program in new_programs:
        kind = "migrant" if program.metadata.get("migrant") else "island-copy"
        identity = json.dumps(
            {
                "kind": kind,
                "parent_id": program.parent_id,
                "island": program.metadata.get("island"),
                "generation": program.generation,
                "iteration_found": program.iteration_found,
                "code_sha256": hashlib.sha256(program.code.encode()).hexdigest(),
            },
            sort_keys=True,
        )
        canonical_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"vibesys-openevolve:{identity}"))
        if canonical_id in database.programs or canonical_id in replacements.values():
            raise RuntimeError(f"duplicate deterministic OpenEvolve program ID: {canonical_id}")  # noqa: TRY003
        replacements[program.id] = canonical_id
        program.id = canonical_id
        program.timestamp = float(program.iteration_found or database.last_iteration)

    if not replacements:
        return
    for old_id, canonical_id in replacements.items():
        program = database.programs.pop(old_id)
        database.programs[canonical_id] = program
    for program in database.programs.values():
        if program.parent_id in replacements:
            program.parent_id = replacements[program.parent_id]
    for island in database.islands:
        for old_id, canonical_id in replacements.items():
            if old_id in island:
                island.discard(old_id)
                island.add(canonical_id)
    for feature_map in database.island_feature_maps:
        for feature, program_id in list(feature_map.items()):
            feature_map[feature] = replacements.get(program_id, program_id)
    database.archive = _SortedIterationSet(
        replacements.get(program_id, program_id) for program_id in database.archive
    )
    if database.best_program_id in replacements:
        database.best_program_id = replacements[database.best_program_id]
    database.island_best_programs = [
        replacements.get(program_id, program_id) if program_id is not None else None
        for program_id in database.island_best_programs
    ]
    if database.prompts_by_program:
        for old_id, canonical_id in replacements.items():
            prompts = database.prompts_by_program.pop(old_id, None)
            if prompts is not None:
                database.prompts_by_program[canonical_id] = prompts


def _resolve_individual(
    database: ProgramDatabase, program: Program, individuals_by_id: dict[int, Individual]
) -> Individual | None:
    current: Program | None = program
    visited: set[str] = set()
    while current is not None and current.id not in visited:
        visited.add(current.id)
        raw_id = current.metadata.get(_INDIVIDUAL_ID)
        if isinstance(raw_id, int):
            return individuals_by_id.get(raw_id)
        current = database.programs.get(current.parent_id) if current.parent_id else None
    return None


def _combined_score(individual: Individual, space: MetricSpace) -> float:
    primary = space.primary
    if primary is not None:
        value = individual.metrics.get(primary.name)
        if value is not None:
            return primary.signed(value)
    if individual.perf_metric is None:
        return 0.0
    return space.signed_primary(individual.perf_metric)


def select(
    state: OpenEvolveSelectorState,
    individuals: tuple[Individual, ...],
    *,
    k_top_inspirations: int,
    k_random_inspirations: int,
    space: MetricSpace,  # noqa: ARG001  # tracked: #288
) -> tuple[Proposal | None, OpenEvolveSelectorState]:
    """Sample a parent/inspirations from the upstream island database."""
    database = _build_database(state.config, seed=None)
    _load_files(database, state.files)
    database.set_current_island(state.current_island)
    rng = random.Random()  # noqa: S311
    rng.setstate(state.rng_state)

    if not database.programs:
        return None, state

    island = database.current_island
    program_ids_before = set(database.programs)
    inspiration_count = k_top_inspirations + k_random_inspirations
    with _upstream_random(rng):
        _normalize_upstream_collections(database)
        parent_program, inspiration_programs = database.sample_from_island(
            island, num_inspirations=inspiration_count
        )
    _canonicalize_new_programs(database, program_ids_before)
    database.next_island()

    individuals_by_id = {individual.id: individual for individual in individuals}
    parent = _resolve_individual(database, parent_program, individuals_by_id)
    new_state = state.model_copy(
        update={
            "files": _dump_files(database, iteration=database.last_iteration),
            "rng_state": cast("RandomState", rng.getstate()),
            "current_island": database.current_island,
        }
    )
    if parent is None:
        return None, new_state

    inspirations: list[Individual] = []
    seen_ids = {parent.id}
    for program in inspiration_programs:
        individual = _resolve_individual(database, program, individuals_by_id)
        if individual is None or individual.id in seen_ids:
            continue
        seen_ids.add(individual.id)
        inspirations.append(individual)

    return (
        Proposal(
            parent=parent,
            inspirations=tuple(inspirations),
            policy_parent_id=parent_program.id,
            target_island=island,
        ),
        new_state,
    )


def admit(  # noqa: PLR0913  # tracked: #288
    state: OpenEvolveSelectorState,
    individual: Individual,
    *,
    code: str,
    policy_parent_id: str | None,
    target_island: int | None,
    space: MetricSpace,  # tracked: #288
) -> OpenEvolveSelectorState:
    """Record one admitted individual into the upstream database."""
    if not individual.passed or not individual.commit:
        return state
    if individual.id in state.admitted_individual_ids:
        return state

    database = _build_database(state.config, seed=None)
    _load_files(database, state.files)
    database.set_current_island(state.current_island)
    rng = random.Random()  # noqa: S311
    rng.setstate(state.rng_state)

    program_id = f"vibesys-{individual.id}"
    if program_id in database.programs:
        return state.model_copy(
            update={"admitted_individual_ids": (*state.admitted_individual_ids, individual.id)}
        )

    metrics = dict(individual.metrics)
    metrics["combined_score"] = _combined_score(individual, space)
    program = Program(
        id=program_id,
        code=code,
        changes_description=individual.summary,
        language="multi-file",
        parent_id=policy_parent_id,
        generation=individual.generation,
        timestamp=float(individual.id),
        metrics=metrics,
        metadata={
            _INDIVIDUAL_ID: individual.id,
            _COMMIT: individual.commit,
            "perf_unit": individual.perf_unit,
        },
    )
    program_ids_before = set(database.programs)
    with _upstream_random(rng):
        _normalize_upstream_collections(database)
        database.add(program, iteration=individual.id, target_island=target_island)
        if individual.generation > 0:
            island = (
                target_island
                if target_island is not None
                else int(program.metadata.get("island", database.current_island))
            )
            database.increment_island_generation(island)
            if state.config.migration_rate > 0.0 and database.should_migrate():
                database.migrate_programs()
    _canonicalize_new_programs(database, program_ids_before)

    return state.model_copy(
        update={
            "files": _dump_files(database, iteration=database.last_iteration),
            "rng_state": cast("RandomState", rng.getstate()),
            "current_island": database.current_island,
            "admitted_individual_ids": (*state.admitted_individual_ids, individual.id),
        }
    )
