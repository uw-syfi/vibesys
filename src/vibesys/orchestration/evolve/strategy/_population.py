"""The population policy as the strategy uses it: proposals, outcomes, fitness and the winner.

`PopulationSearch` is the pure selection and admission policy and is unchanged. This module
binds it to the strategy's records: a generation's proposals are a pure function of the
population as it stood when the generation began, a settled slot becomes a `CandidateOutcome`, and
the best individual's retained settlement is what adoption proposes.
"""

from vibesys.candidates.api import STOPPED, Winner
from vibesys.metrics import AcceptedReading, reading_of
from vibesys.orchestration.evolve.population import (
    CandidateOutcome,
    Individual,
    Metrics,
    PopulationSearch,
    PopulationState,
)
from vibesys.orchestration.evolve.strategy._config import EvolveConfig
from vibesys.orchestration.evolve.strategy._state import (
    EvolveStrategyState,
    ProfileReading,
    Retained,
    SlotProposal,
    SlotState,
)
from vs_core.api import EvidenceKind, RevisionRef


def search_of(config: EvolveConfig) -> PopulationSearch:
    """The run's deterministic selection policy."""
    return PopulationSearch(config.population)


def individual(population: PopulationState, identifier: int) -> Individual | None:
    """The individual with this id."""
    return next((item for item in population.individuals if item.id == identifier), None)


def proposals(config: EvolveConfig, start: PopulationState) -> tuple[SlotProposal | None, ...]:
    """The selector's choices for every slot of a generation that began at ``start``.

    The generator state is threaded through the slots in order and the result is discarded, as
    the legacy loop did, so a given ``--seed`` proposes the same parents and inspirations in
    every generation whatever order the slots complete in.
    """
    search = search_of(config)
    state = start
    chosen: list[SlotProposal | None] = []
    for _ in range(config.children_per_generation):
        proposal, state = search.propose(state)
        chosen.append(
            None
            if proposal is None
            else SlotProposal(
                parent_id=proposal.parent.id,
                inspiration_ids=tuple(item.id for item in proposal.inspirations),
                policy_parent_id=proposal.policy_parent_id,
                target_island=proposal.target_island,
            )
        )
    return tuple(chosen)


def revision_of(state: EvolveStrategyState, member: Individual) -> RevisionRef | None:
    """The revision core retained for an individual, else the one its commit names."""
    for item in state.retained:
        if item.individual == member.id:
            return item.revision
    return RevisionRef.of_git_commit(member.commit) if member.commit else None


def fitness(
    profile: ProfileReading | None, benchmark: AcceptedReading | None
) -> tuple[float | None, str | None, Metrics]:
    """A candidate's recorded fitness: the trusted benchmark over the profiler's self-report.

    The trusted row is merged over the profiler's row rather than replacing it: the scalar
    contract reports one number, so on a two-axis task replacing the row would leave every
    individual incomplete on the second axis, and the frontier (which keeps only individuals
    carrying a value for every configured axis) would return nothing. Without a benchmark the
    profiler's self-report stands; it is never winner-eligible evidence by itself, since core
    grants authority only to trusted evidence.
    """
    metrics = dict(profile.metrics) if profile is not None else {}
    headline = None if benchmark is None else benchmark.headline()
    if benchmark is not None and headline is not None:
        trusted = {row.name: row.value for row in benchmark.metrics}
        unit = headline.unit or (profile.perf_unit if profile is not None else None)
        return headline.value, unit, tuple((metrics | trusted).items())
    return (
        None if profile is None else profile.perf_metric,
        None if profile is None else profile.perf_unit,
        tuple(metrics.items()),
    )


def keeps_commit(slot: SlotState) -> bool:
    """Whether the slot's individual records its revision: a pass, or a cold start's work in progress."""
    return slot.passed or (slot.search_generation == 0 and slot.settled is not None)


def outcome_of(slot: SlotState) -> CandidateOutcome | None:
    """The slot as the population sees it, or None when it earns no individual.

    A candidate that was never measured, one an operator stop cut short and one core never
    admitted earn none; every other settled slot is an individual, a failed one keeping its
    feedback for the lessons later cold starts read.
    """
    settled = slot.settled
    if slot.unmeasured or slot.failure == STOPPED or settled is None:
        return None
    proposal = slot.proposal
    benchmark = reading_of(slot.readings, EvidenceKind.BENCHMARK)
    retained = settled.retained
    commit = retained.revision_id.root if retained is not None and keeps_commit(slot) else None
    metric, unit, metrics = fitness(slot.profile, benchmark) if slot.passed else (None, None, ())
    return CandidateOutcome(
        passed=slot.passed,
        parent_id=None if proposal is None else proposal.parent_id,
        inspiration_ids=() if proposal is None else proposal.inspiration_ids,
        summary=slot.summary,
        feedback=slot.feedback,
        commit=commit,
        perf_metric=metric,
        perf_unit=unit,
        metrics=metrics,
        policy_parent_id=None if proposal is None else proposal.policy_parent_id,
        target_island=None if proposal is None else proposal.target_island,
        code=slot.patch if slot.passed else None,
    )


def admit(state: EvolveStrategyState, config: EvolveConfig, slot: SlotState) -> EvolveStrategyState:
    """Admit one slot's outcome into the population, recording the settlement it rests on."""
    population = state.population
    outcome = outcome_of(slot)
    if population is None or outcome is None:
        return state
    member, grown = search_of(config).admit(population, outcome)
    settled = slot.settled
    retained = state.retained
    if settled is not None and settled.retained is not None and member.commit:
        retained = (
            *retained,
            Retained(
                individual=member.id, settlement=settled.settlement, revision=settled.retained
            ),
        )
    return state.model_copy(update={"population": grown, "retained": retained})


def winner_of(state: EvolveStrategyState, config: EvolveConfig) -> Winner | None:
    """The best individual's settlement, or None when no passing individual rests on one."""
    if state.population is None:
        return None
    best = search_of(config).best(state.population)
    if best is None:
        return None
    for item in state.retained:
        if item.individual == best.id:
            return Winner(
                settlement=item.settlement,
                revision=item.revision,
                hypothesis_id=f"individual-{best.id}",
            )
    return None
