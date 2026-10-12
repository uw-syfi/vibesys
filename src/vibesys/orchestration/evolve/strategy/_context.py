"""Prompt data derived from state and config.

Everything here is a pure function of `EvolveStrategyState` and `EvolveConfig`. Prompt contexts
are typed values; the strategy never writes prompt text.
"""

from vibesys.orchestration.evolve.population import Individual
from vibesys.orchestration.evolve.strategy import _population as population
from vibesys.orchestration.evolve.strategy._config import EvolveConfig
from vibesys.orchestration.evolve.strategy._prompts import (
    JudgePrompt,
    MutatePrompt,
    ObjectiveAxis,
    ProfilerPrompt,
)
from vibesys.orchestration.evolve.strategy._state import EvolveStrategyState, SlotState
from vibesys.orchestration.profilers import ProfilerKind
from vs_core.api import EvidenceRef


def _peers(state: EvolveStrategyState, identifiers: tuple[int, ...]) -> tuple[Individual, ...]:
    """The named individuals as they stood when the generation began."""
    start = state.generation_start
    if start is None:
        return ()
    found = (population.individual(start, item) for item in identifiers)
    return tuple(item for item in found if item is not None)


def mutate_prompt(
    state: EvolveStrategyState, slot: SlotState, config: EvolveConfig
) -> MutatePrompt:
    """The brief of the slot's mutation: a cold start, or a child of a parent."""
    proposal = slot.proposal
    if proposal is None or state.population is None:
        current = state.population
        lessons = (
            () if current is None else tuple(population.search_of(config).failure_lessons(current))
        )
        failed = 0 if current is None else sum(not item.passed for item in current.individuals)
        return MutatePrompt(
            base_revision=slot.base,
            is_cold_start=True,
            repair_seed=slot.repair_seed,
            failed_lessons=lessons,
            num_failed_attempts=failed,
            reference_path=config.reference_path,
            interface=config.interface,
        )
    (parent,) = _peers(state, (proposal.parent_id,))
    return MutatePrompt(
        base_revision=slot.base,
        is_cold_start=False,
        parent=parent,
        inspirations=_peers(state, proposal.inspiration_ids),
        reference_path=config.reference_path,
        interface=config.interface,
    )


def judge_prompt(
    slot: SlotState, config: EvolveConfig, cited_evidence: tuple[EvidenceRef, ...]
) -> JudgePrompt:
    """The review brief for the slot's candidate."""
    if slot.candidate is None:
        message = "a review needs the mutator's candidate"
        raise RuntimeError(message)
    return JudgePrompt(
        candidate=slot.candidate, interface=config.interface, cited_evidence=cited_evidence
    )


def profile_prompt(slot: SlotState, config: EvolveConfig) -> ProfilerPrompt:
    """The profiling brief for the slot's passing candidate."""
    if slot.candidate is None:
        message = "a profile needs the mutator's candidate"
        raise RuntimeError(message)
    return ProfilerPrompt(
        candidate=slot.candidate,
        profiler_kind=ProfilerKind(config.profiler_id).value,
        profile_execution=config.profile_execution,
        benchmark_command=config.benchmark_command,
        objectives=tuple(
            ObjectiveAxis(name=item.name, direction=item.direction)
            for item in config.metric_space.objectives
        ),
        profiler_support_name=config.profiler_support_name,
        profiler_mcp_name=config.profiler_mcp_name,
    )
