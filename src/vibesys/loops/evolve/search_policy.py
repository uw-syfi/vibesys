"""Parent/inspiration search policies for the evolutionary loop.

VibeSys owns candidate materialization and evaluation. Search policies only
choose already-passing individuals and observe newly admitted ones. This keeps
third-party population algorithms independent of the multi-file workspace and
agent execution lifecycle.
"""

from __future__ import annotations

import json
import random
import shutil
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast

from openevolve.config import DatabaseConfig  # pyright: ignore[reportMissingTypeStubs]
from openevolve.database import Program, ProgramDatabase  # pyright: ignore[reportMissingTypeStubs]

from vibesys.loops.evolve.population import Individual, Objective, Population


class SearchPolicyName(StrEnum):
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
class OpenEvolveSearchConfig:
    """Supported OpenEvolve database knobs, pinned to v0.3.1 semantics."""

    population_size: int = 1000
    archive_size: int = 100
    num_islands: int = 5
    migration_interval: int = 50
    migration_rate: float = 0.1

    def __post_init__(self) -> None:
        if self.population_size < 1:
            raise ValueError("OpenEvolve population_size must be >= 1")
        if self.archive_size < 1:
            raise ValueError("OpenEvolve archive_size must be >= 1")
        if self.num_islands < 1:
            raise ValueError("OpenEvolve num_islands must be >= 1")
        if self.migration_interval < 1:
            raise ValueError("OpenEvolve migration_interval must be >= 1")
        if not 0.0 <= self.migration_rate <= 1.0:
            raise ValueError("OpenEvolve migration_rate must be in [0, 1]")


class SearchPolicy(Protocol):
    """Selection/persistence surface consumed by the VibeSys evolve loop."""

    @property
    def requires_code(self) -> bool: ...

    def select(
        self,
        population: Population,
        *,
        rng: random.Random,
        k_top_inspirations: int,
        k_random_inspirations: int,
        selection_temperature: float,
        objectives: list[Objective] | None,
        frontier_bias: float,
    ) -> SearchSelection | None: ...

    def record(
        self,
        individual: Individual,
        *,
        code: str,
        policy_parent_id: str | None,
        target_island: int | None,
        objectives: list[Objective] | None,
    ) -> None: ...

    def finish_generation(self, generation: int) -> None: ...


class VibeSysSearchPolicy:
    """Existing scalar/Pareto population selection."""

    @property
    def requires_code(self) -> bool:
        return False

    def select(
        self,
        population: Population,
        *,
        rng: random.Random,
        k_top_inspirations: int,
        k_random_inspirations: int,
        selection_temperature: float,
        objectives: list[Objective] | None,
        frontier_bias: float,
    ) -> SearchSelection | None:
        parent = population.select_parent(
            rng=rng,
            temperature=selection_temperature,
            objectives=objectives,
            frontier_bias=frontier_bias,
        )
        inspirations = population.select_inspirations(
            parent_id=parent.id if parent else None,
            k_top=k_top_inspirations,
            k_random=k_random_inspirations,
            rng=rng,
            objectives=objectives,
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
        objectives: list[Objective] | None,
    ) -> None:
        return None

    def finish_generation(self, generation: int) -> None:
        return None


class _SortedIterationSet(set[str]):
    """Set semantics with deterministic iteration for replaying OpenEvolve."""

    def __iter__(self):
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
    _STATE_SCHEMA_VERSION = 1

    @property
    def requires_code(self) -> bool:
        return True

    def __init__(
        self,
        *,
        state_dir: Path,
        seed: int | None,
        config: OpenEvolveSearchConfig | None,
        objectives: list[Objective] | None = None,
    ) -> None:
        self.state_dir = state_dir
        self._snapshot_dir = self._resolve_snapshot_dir(state_dir)
        saved_state = self._load_adapter_state()
        saved_config = (
            OpenEvolveSearchConfig(
                **cast(dict[str, Any], saved_state["config"]),
            )
            if saved_state is not None
            else None
        )
        if config is not None and saved_config is not None and config != saved_config:
            raise ValueError(
                "OpenEvolve search configuration does not match the resumed run: "
                f"saved={saved_config}, requested={config}"
            )
        self.config = config or saved_config or OpenEvolveSearchConfig()
        self._objective_signature = self._objectives_signature(objectives)
        saved_objectives = saved_state.get("objective_signature") if saved_state else None
        if saved_objectives is not None and saved_objectives != self._objective_signature:
            raise ValueError(
                "OpenEvolve fitness objective does not match the resumed run: "
                f"saved={saved_objectives}, requested={self._objective_signature}"
            )
        self._admitted_individual_ids = set(
            cast(list[int], saved_state.get("admitted_individual_ids", [])) if saved_state else []
        )
        self._rng = random.Random(seed)
        if saved_state is not None and "rng_state" in saved_state:
            self._rng.setstate(cast(tuple[Any, ...], self._tuple_tree(saved_state["rng_state"])))

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
            active_ids = set(cast(list[str], saved_state.get("active_program_ids", [])))
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
        return cls._resolve_snapshot_dir(state_dir) is not None

    @classmethod
    def persisted_config(cls, state_dir: Path) -> OpenEvolveSearchConfig | None:
        snapshot_dir = cls._resolve_snapshot_dir(state_dir)
        if snapshot_dir is None:
            return None
        state_path = snapshot_dir / cls._STATE_FILE
        payload = json.loads(state_path.read_text())
        if payload.get("schema_version") != cls._STATE_SCHEMA_VERSION:
            raise ValueError(f"unsupported OpenEvolve adapter state in {state_path}")
        return OpenEvolveSearchConfig(**cast(dict[str, Any], payload["config"]))

    @classmethod
    def persisted_objectives(cls, state_dir: Path) -> list[Objective] | None:
        snapshot_dir = cls._resolve_snapshot_dir(state_dir)
        if snapshot_dir is None:
            return None
        state_path = snapshot_dir / cls._STATE_FILE
        payload = json.loads(state_path.read_text())
        if payload.get("schema_version") != cls._STATE_SCHEMA_VERSION:
            raise ValueError(f"unsupported OpenEvolve adapter state in {state_path}")
        signature = cast(list[dict[str, str]], payload.get("objective_signature", []))
        return [Objective(item["name"], item["direction"]) for item in signature]

    @staticmethod
    def _tuple_tree(value: Any) -> Any:
        if isinstance(value, list):
            return tuple(OpenEvolveSearchPolicy._tuple_tree(item) for item in value)
        return value

    def _load_adapter_state(self) -> dict[str, object] | None:
        if self._snapshot_dir is None:
            return None
        state_path = self._snapshot_dir / self._STATE_FILE
        payload = json.loads(state_path.read_text())
        if payload.get("schema_version") != self._STATE_SCHEMA_VERSION:
            raise ValueError(f"unsupported OpenEvolve adapter state in {state_path}")
        return payload

    @classmethod
    def _resolve_snapshot_dir(cls, state_dir: Path) -> Path | None:
        current_path = state_dir / cls._CURRENT_FILE
        if not current_path.is_file():
            return None
        snapshot_name = current_path.read_text().strip()
        snapshot_dir = state_dir / "snapshots" / snapshot_name
        if not snapshot_name or not snapshot_dir.is_dir():
            raise ValueError(f"invalid OpenEvolve snapshot pointer in {current_path}")
        return snapshot_dir

    @staticmethod
    def _objectives_signature(objectives: list[Objective] | None) -> list[dict[str, str]]:
        return [
            {"name": objective.name, "direction": objective.direction}
            for objective in (objectives or [])
        ]

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

    @contextmanager
    def _deterministic_upstream_iteration(self) -> Generator[None, None, None]:
        """Normalize unordered OpenEvolve collections for replayable sampling."""
        self._database.programs = dict(sorted(self._database.programs.items()))
        self._database.islands = [
            island if isinstance(island, _SortedIterationSet) else _SortedIterationSet(island)
            for island in self._database.islands
        ]
        if not isinstance(self._database.archive, _SortedIterationSet):
            self._database.archive = _SortedIterationSet(self._database.archive)
        yield

    def select(
        self,
        population: Population,
        *,
        rng: random.Random,
        k_top_inspirations: int,
        k_random_inspirations: int,
        selection_temperature: float,
        objectives: list[Objective] | None,
        frontier_bias: float,
    ) -> SearchSelection | None:
        del rng, selection_temperature, objectives, frontier_bias
        if not self._database.programs:
            return None

        island = self._database.current_island
        program_ids_before = set(self._database.programs)
        inspiration_count = k_top_inspirations + k_random_inspirations
        with self._upstream_random():
            with self._deterministic_upstream_iteration():
                parent_program, inspiration_programs = self._database.sample_from_island(
                    island,
                    num_inspirations=inspiration_count,
                )
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
        objectives: list[Objective] | None,
    ) -> None:
        if not individual.passed or not individual.commit:
            return
        if self._objectives_signature(objectives) != self._objective_signature:
            raise ValueError("OpenEvolve fitness objective changed during the run")
        if individual.id in self._admitted_individual_ids:
            return
        program_id = f"vibesys-{individual.id}"
        if program_id in self._database.programs:
            prospective_ids = self._admitted_individual_ids | {individual.id}
            self._save_full_state(admitted_individual_ids=prospective_ids)
            self._admitted_individual_ids = prospective_ids
            return
        metrics = dict(individual.metrics)
        metrics["combined_score"] = self._combined_score(individual, objectives)
        program = Program(
            id=program_id,
            code=code,
            changes_description=individual.summary,
            language="multi-file",
            parent_id=policy_parent_id,
            generation=individual.generation,
            metrics=metrics,
            metadata={
                self._INDIVIDUAL_ID: individual.id,
                self._COMMIT: individual.commit,
                "perf_unit": individual.perf_unit,
            },
        )
        with self._upstream_random():
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
        prospective_ids = self._admitted_individual_ids | {individual.id}
        self._save_full_state(admitted_individual_ids=prospective_ids)
        self._admitted_individual_ids = prospective_ids

    def finish_generation(self, generation: int) -> None:
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
    ) -> dict[str, object]:
        active_ids = sorted(self._database.programs)
        return {
            "schema_version": self._STATE_SCHEMA_VERSION,
            "config": asdict(self.config),
            "active_program_ids": active_ids,
            "admitted_individual_ids": sorted(
                admitted_individual_ids or self._admitted_individual_ids
            ),
            "objective_signature": self._objective_signature,
            "rng_state": self._rng.getstate(),
        }

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
                json.dumps(
                    self._adapter_payload(admitted_individual_ids=admitted_individual_ids),
                    sort_keys=True,
                )
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
        for old_snapshot in snapshots_dir.iterdir():
            if old_snapshot != snapshot_dir and old_snapshot.is_dir():
                shutil.rmtree(old_snapshot)

    def _save_selection_state(self) -> None:
        if self._snapshot_dir is None:
            return
        payload = {
            "snapshot": self._snapshot_dir.name,
            "current_island": self._database.current_island,
            "rng_state": self._rng.getstate(),
        }
        selection_path = self.state_dir / self._SELECTION_FILE
        temporary_path = selection_path.with_suffix(".tmp")
        temporary_path.write_text(json.dumps(payload, sort_keys=True))
        temporary_path.replace(selection_path)

    def _restore_selection_state(self) -> None:
        if self._snapshot_dir is None:
            return
        selection_path = self.state_dir / self._SELECTION_FILE
        if not selection_path.is_file():
            return
        payload = json.loads(selection_path.read_text())
        if payload.get("snapshot") != self._snapshot_dir.name:
            return
        self._database.set_current_island(int(payload["current_island"]))
        self._rng.setstate(cast(tuple[Any, ...], self._tuple_tree(payload["rng_state"])))

    @staticmethod
    def _combined_score(
        individual: Individual,
        objectives: list[Objective] | None,
    ) -> float:
        if objectives:
            primary = objectives[0]
            value = individual.metrics.get(primary.name)
            if value is not None:
                return primary.signed(value)
        return float(individual.perf_metric or 0.0)
