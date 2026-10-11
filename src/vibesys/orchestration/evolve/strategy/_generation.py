"""Cold starts and generations: opening slots, and admitting outcomes in slot order.

A cold start is a chain of single-slot attempts, each building on the previous failure's work in
progress, until one passes review and the configured gates. A generation proposes
`children_per_generation` (parent, inspirations) pairs from the population as it stood when the
generation began, so the proposals are a pure function of that value. Children may complete in
any order, but an outcome is admitted only once every lower slot has been settled, in slot order,
and the population then grows exactly as the serial loop's would. That is what makes a replay
reach the same population whatever order the executors finished in.
"""

from vibesys.candidates.api import RunPhase
from vibesys.orchestration.evolve.strategy import _ids as ids
from vibesys.orchestration.evolve.strategy import _population as population
from vibesys.orchestration.evolve.strategy._config import EvolveConfig
from vibesys.orchestration.evolve.strategy._draft import Draft
from vibesys.orchestration.evolve.strategy._state import (
    EvolveStrategyState,
    SlotState,
    Stage,
    WorkPhase,
)


def _new_slot(key: str, slot: int, generation: int, **plan: object) -> SlotState:
    """A slot at its start; ``plan`` is the proposal, base revision and repair flag."""
    return SlotState.model_validate(
        {
            "attempt": ids.attempt_id(key, 1),
            "key": key,
            "slot": slot,
            "search_generation": generation,
            **plan,
        }
    )


def _skipped(slot: SlotState, reason: str) -> SlotState:
    """A slot that cannot start earns no individual."""
    return slot.model_copy(
        update={"phase": WorkPhase.DONE, "failure": reason, "feedback": f"framework: {reason}"}
    )


def open_bootstrap(state: EvolveStrategyState, draft: Draft) -> SlotState:
    """The next cold-start attempt, repairing the last failure's work in progress when it left some."""
    current = state.population
    seed = None if current is None else population.search_of(draft.config).wip_seed(current)
    seeded = None if seed is None else population.revision_of(state, seed)
    number = state.bootstrap_spent + 1
    return _new_slot(
        f"bootstrap.{number}",
        number,
        0,
        base=seeded or draft.view.facts.baseline,
        repair_seed=seeded is not None,
    )


def open_generation(state: EvolveStrategyState, draft: Draft) -> EvolveStrategyState:
    """Begin the next generation: freeze the population and give every slot its proposal."""
    current = state.population
    if current is None:
        return state
    config = draft.config
    started = population.search_of(config).end_generation(current)
    chosen = population.proposals(config, started)
    number = started.generation
    slots: list[SlotState] = []
    for index, proposal in enumerate(chosen, start=1):
        key = f"g{number}.c{index}"
        parent = None if proposal is None else population.individual(started, proposal.parent_id)
        base = None if parent is None else population.revision_of(state, parent)
        slot = _new_slot(
            key, index, number, proposal=proposal, base=base or draft.view.facts.baseline
        )
        slots.append(
            slot if base is not None else _skipped(slot, "the proposal names no retained parent")
        )
    return state.model_copy(
        update={"population": started, "generation_start": started, "slots": tuple(slots)}
    )


def search_over(state: EvolveStrategyState, config: EvolveConfig) -> bool:
    """Whether no slot is open and none will start."""
    if state.phase is not RunPhase.SEARCHING or state.slots or state.population is None:
        return False
    if state.stopping:
        return True
    if state.stage is Stage.BOOTSTRAP:
        return state.bootstrap_spent >= config.bootstrap_max_attempts
    return state.generation_start is None and state.population.generation >= config.max_generations


def decide(draft: Draft) -> None:
    """Open the next cold-start attempt or generation when nothing is in flight."""
    state = draft.state
    if state.phase is not RunPhase.SEARCHING:
        return
    if state.population is None:
        draft.update(population=population.search_of(draft.config).initial())
        state = draft.state
    if state.slots or search_over(state, draft.config) or state.stopping:
        return
    if state.stage is Stage.BOOTSTRAP:
        draft.update(slots=(open_bootstrap(state, draft),))
        return
    draft.state = admit_ready(open_generation(state, draft), draft.config)


def admit_ready(state: EvolveStrategyState, config: EvolveConfig) -> EvolveStrategyState:
    """Admit settled slots in slot order, stopping at the first that is still open.

    Admission is a fold over the slots that are done, lowest first: a child that finished early
    waits in the state until every lower slot has. When the last slot is admitted the
    generation ends; a cold start that produced a passing individual moves the search to its
    first generation.
    """
    slots = list(state.slots)
    for index, slot in enumerate(slots):
        if slot.recorded:
            continue
        if slot.phase is not WorkPhase.DONE:
            break
        state = population.admit(state, config, slot)
        slots[index] = slot.model_copy(update={"recorded": True})
        state = state.model_copy(update={"slots": tuple(slots)})
        if slot.search_generation == 0:
            state = _after_cold_start(state, config)
    if slots and all(item.recorded for item in slots):
        return state.model_copy(update={"slots": (), "generation_start": None})
    return state


def _after_cold_start(state: EvolveStrategyState, config: EvolveConfig) -> EvolveStrategyState:
    """Count the finished cold-start attempt; a passing one starts the generations."""
    current = state.population
    seeded = current is not None and not population.search_of(config).needs_bootstrap(current)
    return state.model_copy(
        update={
            "bootstrap_spent": state.bootstrap_spent + 1,
            "stage": Stage.GENERATION if seeded else state.stage,
        }
    )
