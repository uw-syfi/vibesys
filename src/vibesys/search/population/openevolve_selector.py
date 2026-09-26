"""OpenEvolve selector: upstream MAP-Elites/island database, held as data.

Every call reconstructs OpenEvolve's ``ProgramDatabase`` from
``OpenEvolveSelectorState.files`` (the same relative-path -> JSON-text shape
``ProgramDatabase.save``/``load`` write to a directory, held as in-memory
text instead), mutates it exactly as the upstream algorithm does, then
serializes it back to ``files`` wholesale. ``_load_files``/``_dump_files``
below reimplement ``save``/``load`` field-for-field, reusing the same
serialization helpers (``Program.to_dict``/``from_dict``,
``_serialize_feature_stats``/``_deserialize_feature_stats``,
``_reconstruct_islands``) upstream's own disk path calls, so the transform is
identical -- only the "directory" (a dict) never touches a filesystem. There
is no directory on disk this module owns, and ``files`` always holds the
*complete current* database rather than a growing history of snapshots. This
keeps state size bounded by the database's own size limits
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
import uuid
from contextlib import contextmanager
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

    def __iter__(self):  # noqa: ANN204  # LW-040046 [ANN204]; the special method's return type is the private iterator this class defines.
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
    """Restore ``database`` in place from ``files``, mirroring ``ProgramDatabase.load``.

    Field-for-field equivalent of the upstream disk path: metadata.json's
    keys are assigned exactly as ``load`` assigns them, each programs/*.json
    is parsed with the same ``Program.from_dict``, and island bookkeeping is
    reconstructed with the same ``_reconstruct_islands`` call -- nothing here
    reads a path or opens a file.
    """
    if not files:
        return
    saved_islands: list[list[str]] = []
    metadata_text = files.get("metadata.json")
    if metadata_text is not None:
        metadata = json.loads(metadata_text)
        database.island_feature_maps = metadata.get(
            "island_feature_maps", [{} for _ in range(database.config.num_islands)]
        )
        saved_islands = metadata.get("islands", [])
        database.archive = set(metadata.get("archive", []))
        database.best_program_id = metadata.get("best_program_id")
        database.island_best_programs = metadata.get(
            "island_best_programs", [None] * len(saved_islands)
        )
        database.last_iteration = metadata.get("last_iteration", 0)
        database.current_island = metadata.get("current_island", 0)
        database.island_generations = metadata.get("island_generations", [0] * len(saved_islands))
        database.last_migration_generation = metadata.get("last_migration_generation", 0)
        database.feature_stats = database._deserialize_feature_stats(  # noqa: SLF001  # LW-040047 [SLF001]; this test reads one private attribute to check internal wiring that has no public accessor.
            metadata.get("feature_stats", {})
        )
    for relative, content in files.items():
        if relative == "metadata.json":
            continue
        program = Program.from_dict(json.loads(content))
        database.programs[program.id] = program
    database._reconstruct_islands(saved_islands)  # noqa: SLF001  # LW-040048 [SLF001]; this test reads one private attribute to check internal wiring that has no public accessor.
    if len(database.island_generations) != len(database.islands):
        database.island_generations = [0] * len(database.islands)
    if len(database.island_best_programs) != len(database.islands):
        database.island_best_programs = [None] * len(database.islands)


def _dump_files(database: ProgramDatabase, *, iteration: int) -> dict[str, str]:
    """Serialize ``database`` to ``files``, mirroring ``ProgramDatabase.save``.

    Same field set ``save`` writes (one programs/<id>.json per program, plus
    metadata.json), built from the same ``Program.to_dict``/
    ``_serialize_feature_stats`` calls ``save`` makes -- nothing here creates
    a directory or opens a file. ``save``'s artifact-directory cleanup is
    disk-only housekeeping with no in-memory counterpart (this selector never
    writes ``Program.artifact_dir``), so it has no equivalent here.
    """
    files: dict[str, str] = {
        f"programs/{program.id}.json": json.dumps(program.to_dict())
        for program in database.programs.values()
    }
    files["metadata.json"] = json.dumps(
        {
            "island_feature_maps": database.island_feature_maps,
            "islands": [list(island) for island in database.islands],
            "archive": list(database.archive),
            "best_program_id": database.best_program_id,
            "island_best_programs": database.island_best_programs,
            "last_iteration": iteration or database.last_iteration,
            "current_island": database.current_island,
            "island_generations": database.island_generations,
            "last_migration_generation": database.last_migration_generation,
            "feature_stats": database._serialize_feature_stats(),  # noqa: SLF001  # LW-040049 [SLF001]; this test reads one private attribute to check internal wiring that has no public accessor.
        }
    )
    return files


@contextmanager
def _upstream_random(rng: random.Random) -> Generator[None, None, None]:
    """Isolate OpenEvolve's module-global RNG (and its id generation) behind our own state.

    Upstream mints program ids for migrants and island-reinit copies with
    ``uuid.uuid4()``, which draws from OS entropy rather than the seeded
    ``random`` module. ``_canonicalize_new_programs`` replaces those ids with
    stable, content-derived ones once a call finishes, but *within* a call
    upstream sorts/iterates program sets keyed by the raw, not-yet-canonical
    ids (``_SortedIterationSet`` orders by string value, and
    ``migrate_programs`` sorts island members before picking migrants) --
    whenever two programs tie on fitness, that ordering, and so which one
    wins the tie, would otherwise depend on the OS-random id text rather
    than anything seeded. Routing ``uuid.uuid4`` through the same restored
    ``rng`` stream makes every id upstream mints a deterministic function of
    ``state.rng_state``, closing that gap.
    """
    process_state = random.getstate()
    random.setstate(rng.getstate())
    original_uuid4 = uuid.uuid4

    def _deterministic_uuid4() -> uuid.UUID:
        return uuid.UUID(int=random.getrandbits(128), version=4)

    uuid.uuid4 = _deterministic_uuid4  # ty: ignore[invalid-assignment]
    try:
        yield
    finally:
        uuid.uuid4 = original_uuid4
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


def _canonicalize_new_programs(  # noqa: C901, PLR0912  # LW-040050 [C901, PLR0912]; one sequential pass shares its state across the steps, which helper boundaries would scatter.
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
            message = f"duplicate deterministic OpenEvolve program ID: {canonical_id}"
            raise RuntimeError(message)
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
    space: MetricSpace,  # noqa: ARG001  # LW-040051 [ARG001]; this scripted double accepts the production keyword arguments and ignores the ones it does not need.
) -> tuple[Proposal | None, OpenEvolveSelectorState]:
    """Sample a parent/inspirations from the upstream island database."""
    database = _build_database(state.config, seed=None)
    _load_files(database, state.files)
    database.set_current_island(state.current_island)
    rng = random.Random()  # noqa: S311  # LW-040052 [S311]; the generator only seeds deterministic search sampling and is not used for security.
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


def admit(  # noqa: PLR0913  # LW-040053 [PLR0913]; the parameters are independent injected collaborators or options, and bundling them would hide ownership.
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
    rng = random.Random()  # noqa: S311  # LW-040054 [S311]; the generator only seeds deterministic search sampling and is not used for security.
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
