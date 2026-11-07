"""The multi run's attempt budget covers every attempt its rounds may open.

Guarantee under test: a round opens one attempt for its implementer and, when the run offers a
profiler, one more for the profiler's read-only checkout of the round's base; the run admits
both for every round, so profiling can never exhaust the attempts a later round needs.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.orchestration.multi.search.api import MultiConfig, budget
from vs_sim.api.testing import examples


@settings(max_examples=examples(pr=20, nightly=200))
@given(max_rounds=st.integers(1, 40), profiling=st.booleans())
def test_every_round_may_open_its_implementer_and_profiler_attempts(
    *, max_rounds: int, profiling: bool
) -> None:
    config = MultiConfig.model_validate({"max_rounds": max_rounds, "profiling": profiling})

    attempts_per_round = 2 if profiling else 1
    assert budget(config).attempts == max_rounds * attempts_per_round
