"""Evolutionary-search orchestrator and committed-state projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.context import RunSetup, RunStartHints
from vibesys.evaluators.metrics import MetricSpace
from vibesys.loops.evolve.orchestration import compare_resume, options_from_descriptor
from vibesys.loops.evolve.run import EvolveRun
from vibesys.loops.evolve.state import EvolutionProjection, EvolutionStateStore, EvolveState
from vibesys.orchestration.view import RunStatus, RunView
from vibesys.search.population.models import PopulationConfig
from vibesys.search.population.search import PopulationSearch

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.orchestration.runtime import RunContext
    from vs_project.api import OrchestrationDescriptor, Project


class _UncommittedSelectionError(RuntimeError):
    def __init__(self, individual_id: int) -> None:
        super().__init__(f"selected individual {individual_id} has no Git commit")


def _empty_state() -> EvolveState:
    """The state a reader sees before this run has committed anything.

    Pinned to a fixed seed (never the real run's, which comes from
    ``EvolveOptions.seed`` once ``EvolveRun.open`` actually starts the run):
    a placeholder read before anything is committed must be idempotent, so
    repeated reads of the same not-yet-started run agree with each other
    instead of each redrawing a fresh OS-entropy RNG state.
    """
    return EvolveState(population=PopulationSearch(PopulationConfig(seed=0)).initial())


class EvolveOrchestrator:
    """Bootstrap, evaluate each generation, then select the final candidate."""

    def __init__(self, descriptor: OrchestrationDescriptor) -> None:
        """Validate evolve settings before the run host opens resources."""
        self.options = options_from_descriptor(descriptor)
        self.setup = RunSetup(
            state_namespace="evolve",
            state_slots={"state.json": EvolveState, "metrics.json": MetricSpace},
            resume_policy=compare_resume,
            start_hints=RunStartHints(
                max_rounds=self.options.max_generations,
                expected_roles=("implementer", "judge"),
            ),
        )

    async def run(self, ctx: RunContext) -> bool:
        """Run the full generation budget through the shared host."""
        run = await EvolveRun.open(ctx, self.options)
        if not await self._bootstrap_if_needed(ctx, run):
            return False

        run.report_parallel_mode()
        for generation in range(run.next_generation, self.options.max_generations + 1):
            await ctx.control.boundary()
            await run.run_generation(generation)

        best = run.search.best(run.state.population)
        if best is not None:
            if best.commit is None:
                raise _UncommittedSelectionError(best.id)
            await ctx.workspaces.adopt(best.commit)
            await ctx.workspaces.root.snapshot(f"evolve: select individual {best.id}")
        run.report_final(best)
        return True

    @staticmethod
    async def _bootstrap_if_needed(ctx: RunContext, run: EvolveRun) -> bool:
        """Produce the first passing seed before sampling descendants."""
        if not run.search.needs_bootstrap(run.state.population):
            return True
        await ctx.control.boundary()
        if await run.bootstrap() is not None:
            return True
        ctx.log("[evolutionary] bootstrap produced no passing seed.")
        return False


@dataclass(frozen=True, slots=True)
class EvolveProjector:
    """Project the same committed population for live and historical readers."""

    def view(self, project: Project, run_id: str, *, status: RunStatus, loop: str) -> RunView:
        """Read the committed evolve namespace for historical observation."""
        store = EvolutionStateStore(project.state.portable_namespace(run_id, "evolve"))
        state = store.load() or _empty_state()
        return self._view(store.projection(state), run_id=run_id, status=status, loop=loop)

    def project_committed(self, namespace: str, state: BaseModel, *, run_id: str) -> RunView | None:
        """Project a newly committed evolve state into the live run view."""
        if namespace != "evolve" or not isinstance(state, EvolutionProjection):
            return None
        return self._view(state, run_id=run_id, status=RunStatus.ACTIVE, loop="evolve")

    @staticmethod
    def _view(state: EvolutionProjection, *, run_id: str, status: RunStatus, loop: str) -> RunView:
        return RunView(
            run_id=run_id,
            loop=loop,
            status=status,
            projection=state.model_dump(mode="json"),
        )
