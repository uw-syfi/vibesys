"""Executor observation contract: core must accept an executor's outputs in order.

An executor's output for one request can change over time: Unknown or a
retryable failure first, then success, then replays of the stored result after
restarts. Core accepts a later observation only through ``fresh_observation``,
so the contract is that every output, in the order it was produced, is proven
fresh against the outputs before it. Each executor kind registers a scenario
that produces such a sequence and passes it to ``assert_core_accepts``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.api.proofs import Proven, fresh_observation

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_core.api import Observation
    from vs_runtime.api.core import ExecutionResult


def assert_core_accepts(outputs: Sequence[ExecutionResult]) -> None:
    """Require the outputs, first non-terminal and last terminal, to be proven fresh in order."""
    assert len(outputs) >= 2, "a scenario needs a retry: at least two outputs"
    observations = [result.observation.observation for result in outputs]
    assert not observations[0].terminal, "the first output must be Unknown or retryable"
    assert observations[-1].terminal, "the last output must be a terminal result"
    history: list[Observation] = []
    for result, observation in zip(outputs, observations, strict=True):
        for event in result.owner_events:
            owner_observation = getattr(event, "observation", None)
            if owner_observation is not None:
                assert owner_observation == observation, "owner event carries another observation"
        verdict = fresh_observation(tuple(history), observation, complete=True)
        assert isinstance(verdict, Proven), (
            f"core rejects observation {observation.event_id.root} "
            f"(sequence {observation.sequence}) after {[row.sequence for row in history]}: "
            f"{verdict}"
        )
        history.append(observation)
