"""A generation completes, or the run ends typed, whichever way its slots reach done."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.evolve.strategy.world import (
    FACTS,
    EvolveWorld,
    evolve_config,
    run_to_end,
)

from vibesys.orchestration.evolve.strategy.api import (
    EvolveStrategy,
    EvolveStrategyState,
    SlotState,
    decision_id,
)
from vs_core.api import Rejected, RejectionCode, attempt_id, project

RUNS = settings(max_examples=40, deadline=None, derandomize=True, database=None)


def test_a_child_core_refuses_to_start_does_not_stall_the_run() -> None:
    # The cold start takes the only attempt core grants; g1.c1's start comes back rejected.
    run_to_end(EvolveWorld(children_per_generation=1, max_generations=1, attempt_cap=1))


@RUNS
@given(
    children=st.integers(1, 3),
    parallelism=st.integers(1, 3),
    generations=st.integers(1, 2),
    cap=st.none() | st.integers(1, 6),
    stop_at=st.none() | st.integers(0, 80),
)
def test_every_way_a_slot_ends_leads_to_a_typed_terminal_state(
    children: int, parallelism: int, generations: int, cap: int | None, stop_at: int | None
) -> None:
    """Starts core refuses and a stop at any request never leave the run waiting on a slot."""
    run_to_end(
        EvolveWorld(
            children_per_generation=children,
            max_parallelism=parallelism,
            max_generations=generations,
            attempt_cap=cap,
            stop_at=stop_at,
        )
    )


def test_a_rejected_settlement_ends_the_slot_instead_of_stranding_it() -> None:
    """Core refusing a `Withdraw` sends no `AttemptSettled`; the slot must still be admitted."""
    config = evolve_config(children_per_generation=1)
    view = project(run_to_end(EvolveWorld(max_generations=1)).core)
    settle = decision_id("settle", "g1.c1")
    slot = SlotState.model_validate(
        {
            "attempt": attempt_id("g1.c1", 1),
            "key": "g1.c1",
            "slot": 1,
            "search_generation": 1,
            "base": FACTS.baseline,
            "phase": "settle",
            "step": "awaiting",
            "awaiting": settle,
            "ready": True,
            "generation": 1,
            "settle_sent": True,
        },
        strict=False,  # the phase and step enums are not public, so they are given by value
    )
    strategy = EvolveStrategy(
        config=config, state=EvolveStrategyState(population=None, slots=(slot,))
    )
    rejected = Rejected(
        decision_id=settle, code=RejectionCode.STALE_VIEW, path=("target",), detail="refused"
    )

    after = strategy.bind(strategy.on_event(view, rejected))
    proposal = after.decide(view)

    assert all(item.key != "g1.c1" for item in proposal.state.slots)
