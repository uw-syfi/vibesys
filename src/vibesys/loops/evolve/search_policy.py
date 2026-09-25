"""Parent/inspiration search policies for the evolutionary loop.

VibeSys owns candidate materialization and evaluation. Search policies only
choose already-passing individuals and observe newly admitted ones. This keeps
third-party population algorithms independent of the multi-file workspace and
agent execution lifecycle.
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol, cast

from openevolve.config import DatabaseConfig
from openevolve.database import Program, ProgramDatabase
from pydantic import BaseModel, ConfigDict, Field

from vibesys.evaluators.metrics import MetricSpace, Objective

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator
    from pathlib import Path

    from vibesys.loops.evolve.population import Individual, Population


class SearchPolicyName(StrEnum):
    """Registered search algorithms for evolutionary loops."""

    VIBESYS = "vibesys"
    OPENEVOLVE = "openevolve"


@dataclass(frozen=True)
class SearchSelection:
    """A materializable VibeSys selection plus policy-specific lineage data."""

    parent: Individual
    inspirations: list[Individual]
    policy_parent_id: str | None = None
    target_island: int | None = None


@dataclass(frozen=True)
class SearchSelectionParameters:
    """Independent controls shared by the registered search policies."""

    rng: random.Random
    k_top_inspirations: int
    k_random_inspirations: int
    selection_temperature: float
    space: MetricSpace
    frontier_bias: float


@dataclass(frozen=True)
class OpenEvolveSearchConfig:
    """Supported OpenEvolve database knobs, pinned to v0.3.1 semantics."""

    population_size: int = 1000
    archive_size: int = 100
    num_islands: int = 5
    migration_interval: int = 50
    migration_rate: float = 0.1

    def __post_init__(self) -> None:
        """Validate OpenEvolve configuration bounds at construction."""
        if self.population_size < 1:
            message = "OpenEvolve population_size must be >= 1"
            raise ValueError(message)
        if self.archive_size < 1:
            message = "OpenEvolve archive_size must be >= 1"
            raise ValueError(message)
        if self.num_islands < 1:
            message = "OpenEvolve num_islands must be >= 1"
            raise ValueError(message)
        if self.migration_interval < 1:
            message = "OpenEvolve migration_interval must be >= 1"
            raise ValueError(message)
        if not 0.0 <= self.migration_rate <= 1.0:
            message = "OpenEvolve migration_rate must be in [0, 1]"
            raise ValueError(message)


class _PersistedOpenEvolveConfig(BaseModel):
    """Strict JSON representation of supported OpenEvolve settings."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    population_size: int = Field(gt=0)
    archive_size: int = Field(gt=0)
    num_islands: int = Field(gt=0)
    migration_interval: int = Field(gt=0)
    migration_rate: float = Field(ge=0, le=1, allow_inf_nan=False)

    @classmethod
    def from_domain(cls, config: OpenEvolveSearchConfig) -> _PersistedOpenEvolveConfig:
        return cls(
            population_size=config.population_size,
            archive_size=config.archive_size,
            num_islands=config.num_islands,
            migration_interval=config.migration_interval,
            migration_rate=config.migration_rate,
        )

    def to_domain(self) -> OpenEvolveSearchConfig:
        return OpenEvolveSearchConfig(**self.model_dump())


class _PersistedObjective(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str
    direction: Literal["max", "min"]


type _RandomState = tuple[int, tuple[int, ...], float | None]


class _AdapterState(BaseModel):
    """VibeSys-owned metadata stored beside an upstream database snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    config: _PersistedOpenEvolveConfig
    active_program_ids: tuple[str, ...]
    admitted_individual_ids: tuple[int, ...]
    objective_signature: tuple[_PersistedObjective, ...]
    rng_state: _RandomState


class _SelectionState(BaseModel):
    """Lightweight state that advances between full upstream snapshots."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    snapshot: str
    current_island: int = Field(ge=0)
    rng_state: _RandomState


class SearchPolicy(Protocol):
    """Selection/persistence surface consumed by the VibeSys evolve loop."""

    @property
    def requires_code(self) -> bool:
        """Whether selections require candidate source code."""
        ...

    def select(
        self,
        population: Population,
        parameters: SearchSelectionParameters,
    ) -> SearchSelection | None:
        """Select a parent and optional inspirations for the next candidate."""
        ...

    def record(
        self,
        individual: Individual,
        *,
        code: str,
        policy_parent_id: str | None,
        target_island: int | None,
        space: MetricSpace,
    ) -> None:
        """Record a candidate admitted by the evolve loop."""
        ...

    def finish_generation(self, generation: int) -> None:
        """Persist any state accumulated during the completed generation."""
        ...


class VibeSysSearchPolicy:
    """Existing scalar/Pareto population selection."""

    @property
    def requires_code(self) -> bool:
        """Scalar/Pareto selection uses persisted individuals, not source."""
        return False

    def select(
        self,
        population: Population,
        parameters: SearchSelectionParameters,
    ) -> SearchSelection | None:
        """Select a parent and inspiration set from the current population."""
        parent = population.select_parent(
            rng=parameters.rng,
            temperature=parameters.selection_temperature,
            space=parameters.space,
            frontier_bias=parameters.frontier_bias,
        )
        inspirations = population.select_inspirations(
            parent_id=parent.id if parent else None,
            k_top=parameters.k_top_inspirations,
            k_random=parameters.k_random_inspirations,
            rng=parameters.rng,
            space=parameters.space,
        )
        if parent is None:
            passers = population.passed
            if not passers:
                return None
            parent = passers[-1]
        return SearchSelection(parent=parent, inspirations=inspirations)

    def record(
        self,
        individual: Individual,
        *,
        code: str,
        policy_parent_id: str | None,
        target_island: int | None,
        space: MetricSpace,
    ) -> None:
        """Accept an individual without maintaining policy-specific state."""
        del individual, code, policy_parent_id, target_island, space

    def finish_generation(self, generation: int) -> None:
        """Finish a generation without additional persistence."""
        del generation


class _SortedIterationSet(set[str]):
    """Set semantics with deterministic iteration for replaying OpenEvolve."""

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(super().__iter__()))


class OpenEvolveSearchPolicy:
    """Adapter around OpenEvolve 0.3.1's MAP-Elites/island database.

    ``Program.code`` is a canonical multi-file git patch supplied by VibeSys.
    Program metadata points back to the durable VibeSys individual, whose git
    commit remains the source of truth for materializing a candidate.
    """

    _INDIVIDUAL_ID = "vibesys_individual_id"
    _COMMIT = "vibesys_commit"
    _STATE_FILE = "adapter.json"
    _CURRENT_FILE = "CURRENT"
    _SELECTION_FILE = "selection.json"
    _STATE_SCHEMA_VERSION: Literal[1] = 1

    @property
    def requires_code(self) -> bool:
        """OpenEvolve selection stores candidate source patches."""
        return True

    def __init__(
        self,
        *,
        state_dir: Path,
        seed: int | None,
        config: OpenEvolveSearchConfig | None,
        space: MetricSpace,
    ) -> None:
        """Restore or initialize the OpenEvolve adapter in its state directory."""
        self.state_dir = state_dir
        self._snapshot_dir = self._resolve_snapshot_dir(state_dir)
        saved_state = self._load_adapter_state()
        saved_config = saved_state.config.to_domain() if saved_state is not None else None
        if config is not None and saved_config is not None and config != saved_config:
            message = (
                "OpenEvolve search configuration does not match the resumed run: "
                f"saved={saved_config}, requested={config}"
            )
            raise ValueError(message)
        self.config = config or saved_config or OpenEvolveSearchConfig()
        self._objective_signature = self._objectives_signature(space)
        saved_objectives = saved_state.objective_signature if saved_state else None
        if saved_objectives is not None and saved_objectives != self._objective_signature:
            message = (
                "OpenEvolve fitness objective does not match the resumed run: "
                f"saved={saved_objectives}, requested={self._objective_signature}"
            )
            raise ValueError(message)
        self._admitted_individual_ids = set(
            saved_state.admitted_individual_ids if saved_state else ()
        )
        self._rng = random.Random(seed)  # noqa: S311  # lint-waiver: LW-009042 [S311]; seeded pseudorandom state makes policy selection replayable, with no security token use.
        if saved_state is not None:
            self._rng.setstate(saved_state.rng_state)

        process_random_state = random.getstate()
        try:
            self._database = ProgramDatabase(
                DatabaseConfig(
                    db_path=None,
                    in_memory=True,
                    log_prompts=False,
                    population_size=self.config.population_size,
                    archive_size=self.config.archive_size,
                    num_islands=self.config.num_islands,
                    feature_dimensions=["complexity", "diversity"],
                    migration_interval=self.config.migration_interval,
                    migration_rate=self.config.migration_rate,
                    random_seed=seed,
                )
            )
            if self._snapshot_dir is not None:
                self._database.load(str(self._snapshot_dir))
        finally:
            random.setstate(process_random_state)

        if saved_state is not None:
            active_ids = set(saved_state.active_program_ids)
            self._database.programs = {
                program_id: program
                for program_id, program in self._database.programs.items()
                if program_id in active_ids
            }
            self._restore_selection_state()
        else:
            self._save_full_state()

    @classmethod
    def has_state(cls, state_dir: Path) -> bool:
        """Return whether an OpenEvolve snapshot exists in the state directory."""
        return cls._resolve_snapshot_dir(state_dir) is not None

    @classmethod
    def persisted_config(cls, state_dir: Path) -> OpenEvolveSearchConfig | None:
        """Load the persisted OpenEvolve configuration, if present."""
        snapshot_dir = cls._resolve_snapshot_dir(state_dir)
        if snapshot_dir is None:
            return None
        return cls._read_adapter_state(snapshot_dir).config.to_domain()

    @classmethod
    def persisted_objectives(cls, state_dir: Path) -> list[Objective] | None:
        """Load the persisted objective signature, if present."""
        snapshot_dir = cls._resolve_snapshot_dir(state_dir)
        if snapshot_dir is None:
            return None
        signature = cls._read_adapter_state(snapshot_dir).objective_signature
        return [Objective(item.name, item.direction) for item in signature]

    def _load_adapter_state(self) -> _AdapterState | None:
        if self._snapshot_dir is None:
            return None
        return self._read_adapter_state(self._snapshot_dir)

    @classmethod
    def _read_adapter_state(cls, snapshot_dir: Path) -> _AdapterState:
        state_path = snapshot_dir / cls._STATE_FILE
        return _AdapterState.model_validate_json(state_path.read_bytes(), strict=True)

    @classmethod
    def _resolve_snapshot_dir(cls, state_dir: Path) -> Path | None:
        current_path = state_dir / cls._CURRENT_FILE
        if not current_path.is_file():
            return None
        snapshot_name = current_path.read_text().strip()
        snapshot_dir = state_dir / "snapshots" / snapshot_name
        if not snapshot_name or not snapshot_dir.is_dir():
            message = f"invalid OpenEvolve snapshot pointer in {current_path}"
            raise ValueError(message)
        return snapshot_dir

    @staticmethod
    def _objectives_signature(
        space: MetricSpace,
    ) -> tuple[_PersistedObjective, ...]:
        return tuple(
            _PersistedObjective(name=objective.name, direction=objective.direction)
            for objective in space.objectives
        )

    @contextmanager
    def _upstream_random(self) -> Generator[None, None, None]:
        """Isolate OpenEvolve's module-global RNG and preserve it on resume."""
        process_state = random.getstate()
        random.setstate(self._rng.getstate())
        try:
            yield
        finally:
            self._rng.setstate(random.getstate())
            random.setstate(process_state)

    def _normalize_upstream_collections(self) -> None:
        """Normalize unordered OpenEvolve collections for replayable operations."""
        self._database.programs = dict(sorted(self._database.programs.items()))
        self._database.islands = [
            island if isinstance(island, _SortedIterationSet) else _SortedIterationSet(island)
            for island in self._database.islands
        ]
        if not isinstance(self._database.archive, _SortedIterationSet):
            self._database.archive = _SortedIterationSet(self._database.archive)

    def _canonicalize_new_upstream_programs(self, program_ids_before: set[str]) -> None:
        """Replace upstream random IDs and timestamps with state-derived values."""
        new_programs = [
            program
            for program_id, program in self._database.programs.items()
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
            if canonical_id in self._database.programs or canonical_id in replacements.values():
                message = f"duplicate deterministic OpenEvolve program ID: {canonical_id}"
                raise RuntimeError(message)
            replacements[program.id] = canonical_id
            program.id = canonical_id
            program.timestamp = float(program.iteration_found or self._database.last_iteration)

        if not replacements:
            return

        self._replace_program_ids(replacements)

    def _replace_program_ids(self, replacements: dict[str, str]) -> None:
        """Update every upstream index when canonicalizing program identities."""
        self._rekey_programs(replacements)
        self._rekey_islands(replacements)
        self._rekey_history(replacements)
        self._rekey_prompts(replacements)

    def _rekey_programs(self, replacements: dict[str, str]) -> None:
        for old_id, canonical_id in replacements.items():
            program = self._database.programs.pop(old_id)
            self._database.programs[canonical_id] = program
        for program in self._database.programs.values():
            if program.parent_id in replacements:
                program.parent_id = replacements[program.parent_id]

    def _rekey_islands(self, replacements: dict[str, str]) -> None:
        for island in self._database.islands:
            for old_id, canonical_id in replacements.items():
                if old_id in island:
                    island.discard(old_id)
                    island.add(canonical_id)
        for feature_map in self._database.island_feature_maps:
            for feature, program_id in list(feature_map.items()):
                feature_map[feature] = replacements.get(program_id, program_id)

    def _rekey_history(self, replacements: dict[str, str]) -> None:
        self._database.archive = _SortedIterationSet(
            replacements.get(program_id, program_id) for program_id in self._database.archive
        )
        if self._database.best_program_id in replacements:
            self._database.best_program_id = replacements[self._database.best_program_id]
        self._database.island_best_programs = [
            replacements.get(program_id, program_id) if program_id is not None else None
            for program_id in self._database.island_best_programs
        ]

    def _rekey_prompts(self, replacements: dict[str, str]) -> None:
        if self._database.prompts_by_program:
            for old_id, canonical_id in replacements.items():
                prompts = self._database.prompts_by_program.pop(old_id, None)
                if prompts is not None:
                    self._database.prompts_by_program[canonical_id] = prompts

    def select(
        self,
        population: Population,
        parameters: SearchSelectionParameters,
    ) -> SearchSelection | None:
        """Select a parent program and inspirations from the active island."""
        if not self._database.programs:
            return None

        island = self._database.current_island
        program_ids_before = set(self._database.programs)
        inspiration_count = parameters.k_top_inspirations + parameters.k_random_inspirations
        with self._upstream_random():
            self._normalize_upstream_collections()
            parent_program, inspiration_programs = self._database.sample_from_island(
                island,
                num_inspirations=inspiration_count,
            )
        self._canonicalize_new_upstream_programs(program_ids_before)
        self._database.next_island()

        individuals_by_id = {individual.id: individual for individual in population.passed}
        parent = self._resolve_individual(parent_program, individuals_by_id)
        if parent is None:
            self._save_after_selection(program_ids_before)
            return None

        inspirations: list[Individual] = []
        seen_ids = {parent.id}
        for program in inspiration_programs:
            individual = self._resolve_individual(program, individuals_by_id)
            if individual is None or individual.id in seen_ids:
                continue
            seen_ids.add(individual.id)
            inspirations.append(individual)

        self._save_after_selection(program_ids_before)
        return SearchSelection(
            parent=parent,
            inspirations=inspirations,
            policy_parent_id=parent_program.id,
            target_island=island,
        )

    def record(
        self,
        individual: Individual,
        *,
        code: str,
        policy_parent_id: str | None,
        target_island: int | None,
        space: MetricSpace,
    ) -> None:
        """Add an accepted individual to the OpenEvolve database."""
        if not individual.passed or not individual.commit:
            return
        if self._objectives_signature(space) != self._objective_signature:
            message = "OpenEvolve fitness objective changed during the run"
            raise ValueError(message)
        if individual.id in self._admitted_individual_ids:
            return
        program_id = f"vibesys-{individual.id}"
        if program_id in self._database.programs:
            prospective_ids = self._admitted_individual_ids | {individual.id}
            self._save_full_state(admitted_individual_ids=prospective_ids)
            self._admitted_individual_ids = prospective_ids
            return
        metrics = dict(individual.metrics)
        metrics["combined_score"] = self._combined_score(individual, space)
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
                self._INDIVIDUAL_ID: individual.id,
                self._COMMIT: individual.commit,
                "perf_unit": individual.perf_unit,
            },
        )
        program_ids_before = set(self._database.programs)
        with self._upstream_random():
            self._normalize_upstream_collections()
            self._database.add(
                program,
                iteration=individual.id,
                target_island=target_island,
            )
            if individual.generation > 0:
                island = (
                    target_island
                    if target_island is not None
                    else int(program.metadata.get("island", self._database.current_island))
                )
                self._database.increment_island_generation(island)
                if self.config.migration_rate > 0.0 and self._database.should_migrate():
                    self._database.migrate_programs()
        self._canonicalize_new_upstream_programs(program_ids_before)
        prospective_ids = self._admitted_individual_ids | {individual.id}
        self._save_full_state(admitted_individual_ids=prospective_ids)
        self._admitted_individual_ids = prospective_ids

    def finish_generation(self, generation: int) -> None:
        """Persist selection state after completing a generation."""
        del generation
        self._save_selection_state()

    def _resolve_individual(
        self,
        program: Program,
        individuals_by_id: dict[int, Individual],
    ) -> Individual | None:
        current: Program | None = program
        visited: set[str] = set()
        while current is not None and current.id not in visited:
            visited.add(current.id)
            raw_id = current.metadata.get(self._INDIVIDUAL_ID)
            if isinstance(raw_id, int):
                individual = individuals_by_id.get(raw_id)
                if individual is not None and current is not program:
                    program.metadata[self._INDIVIDUAL_ID] = raw_id
                    program.metadata[self._COMMIT] = individual.commit
                return individual
            current = self._database.programs.get(current.parent_id) if current.parent_id else None
        return None

    def _adapter_payload(
        self,
        *,
        admitted_individual_ids: set[int] | None = None,
    ) -> _AdapterState:
        active_ids = sorted(self._database.programs)
        return _AdapterState(
            schema_version=self._STATE_SCHEMA_VERSION,
            config=_PersistedOpenEvolveConfig.from_domain(self.config),
            active_program_ids=tuple(active_ids),
            admitted_individual_ids=tuple(
                sorted(admitted_individual_ids or self._admitted_individual_ids)
            ),
            objective_signature=self._objective_signature,
            rng_state=cast("_RandomState", self._rng.getstate()),
        )

    def _save_after_selection(self, program_ids_before: set[str]) -> None:
        if set(self._database.programs) != program_ids_before:
            self._save_full_state()
        else:
            self._save_selection_state()

    def _save_full_state(self, *, admitted_individual_ids: set[int] | None = None) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        snapshots_dir = self.state_dir / "snapshots"
        snapshots_dir.mkdir(exist_ok=True)
        snapshot_name = f"{self._database.last_iteration}-{uuid.uuid4().hex}"
        temporary_dir = snapshots_dir / f".{snapshot_name}.tmp"
        snapshot_dir = snapshots_dir / snapshot_name
        temporary_dir.mkdir()
        try:
            self._database.save(str(temporary_dir))
            (temporary_dir / self._STATE_FILE).write_text(
                self._adapter_payload(
                    admitted_individual_ids=admitted_individual_ids
                ).model_dump_json()
            )
            temporary_dir.replace(snapshot_dir)
            current_path = self.state_dir / self._CURRENT_FILE
            temporary_current = current_path.with_suffix(".tmp")
            temporary_current.write_text(snapshot_name)
            temporary_current.replace(current_path)
        finally:
            if temporary_dir.exists():
                shutil.rmtree(temporary_dir)

        self._snapshot_dir = snapshot_dir
        self._save_selection_state()
        # Earlier snapshots may already be committed as portable framework
        # metadata. Keep them immutable; CURRENT selects the active snapshot.

    def _save_selection_state(self) -> None:
        if self._snapshot_dir is None:
            return
        state = _SelectionState(
            snapshot=self._snapshot_dir.name,
            current_island=self._database.current_island,
            rng_state=cast("_RandomState", self._rng.getstate()),
        )
        selection_path = self.state_dir / self._SELECTION_FILE
        temporary_path = selection_path.with_suffix(".tmp")
        temporary_path.write_text(state.model_dump_json())
        temporary_path.replace(selection_path)

    def _restore_selection_state(self) -> None:
        if self._snapshot_dir is None:
            return
        selection_path = self.state_dir / self._SELECTION_FILE
        if not selection_path.is_file():
            return
        state = _SelectionState.model_validate_json(selection_path.read_bytes(), strict=True)
        if state.snapshot != self._snapshot_dir.name:
            return
        self._database.set_current_island(state.current_island)
        self._rng.setstate(state.rng_state)

    @staticmethod
    def _combined_score(
        individual: Individual,
        space: MetricSpace,
    ) -> float:
        primary = space.primary
        if primary is not None:
            value = individual.metrics.get(primary.name)
            if value is not None:
                # A ranking score for the upstream database, not a comparison:
                # it must stay monotone in the primary axis.
                return primary.signed(value)
        if individual.perf_metric is None:
            return 0.0
        # The profiler contract defines perf_metric as the configured primary's
        # headline value when an objective space exists.
        return space.signed_primary(individual.perf_metric)
