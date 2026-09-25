"""Evolutionary-search orchestrator and committed-state projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.context import RunSetup, RunStartHints
from vibesys.evaluators.metrics import MetricSpace
from vibesys.loops.evolve.orchestration import compare_resume, options_from_descriptor
from vibesys.loops.evolve.run import EvolveRun
from vibesys.loops.evolve.state import (
    EvolutionProjection,
    EvolutionStateStore,
    GenerationCursor,
    restore_uncommitted_evolve_state,
)
from vibesys.orchestration.view import RunStatus, RunView
from vs_loop_state.api import PopulationSnapshot

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.orchestration.runtime import RunContext
    from vs_project.api import OrchestrationDescriptor, Project


class _UncommittedSelectionError(RuntimeError):
    def __init__(self, individual_id: int) -> None:
        super().__init__(f"selected individual {individual_id} has no Git commit")


class EvolveOrchestrator:
    """Bootstrap, evaluate each generation, then select the final candidate."""

    def __init__(self, descriptor: OrchestrationDescriptor) -> None:
        """Validate evolve settings before the run host opens resources."""
        self.options = options_from_descriptor(descriptor)
        self.setup = RunSetup(
            state_namespace="evolve",
            state_slots={
                "population.json": PopulationSnapshot,
                "metrics.json": MetricSpace,
                "generation.json": GenerationCursor,
            },
            resume_policy=compare_resume,
            resume_recovery=restore_uncommitted_evolve_state,
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

        parallel = run.parallel
        run.report_parallel_mode()
        for generation in range(run.first_generation, self.options.max_generations + 1):
            await ctx.control.boundary()
            await run.begin_generation(generation)
            if parallel:
                plans = await run.plan_generation(generation)
                outcomes = await run.evaluate_parallel(generation, plans)
                for child_idx in range(
                    run.next_child(generation), self.options.children_per_generation + 1
                ):
                    await run.record_candidate(
                        generation,
                        child_idx,
                        outcomes.get(child_idx),
                        serial=False,
                    )
            else:
                for child_idx in range(
                    run.next_child(generation), self.options.children_per_generation + 1
                ):
                    await ctx.control.boundary()
                    with run.candidate_progress(generation, child_idx):
                        plan = await run.plan_candidate(generation, child_idx)
                        if plan is None:
                            continue
                        outcome = await run.evaluate_candidate(generation, child_idx, plan)
                        await run.record_candidate(generation, child_idx, outcome, serial=True)
            await run.complete_generation(generation)

        best = run.search.final_choice()
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
        if not run.search.needs_bootstrap():
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
        state = EvolutionStateStore(project.state.portable_namespace(run_id, "evolve"))
        return self._view(state.projection(), run_id=run_id, status=status, loop=loop)

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
