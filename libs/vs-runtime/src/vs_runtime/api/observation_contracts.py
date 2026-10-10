"""Executor observation contract: core must accept an executor's outputs in order.

An executor's output for one request can change over time: Unknown or a
retryable failure first, then success, then replays of the stored result after
restarts. Core accepts a later observation only through ``fresh_observation``,
so the contract is that every observation an executor emits, in the order it
was produced, is proven fresh against the earlier observations of the same
request. Each executor kind registers a scenario that produces such a sequence
and passes the results to ``assert_core_accepts``.

Observations of other requests inside a result (an Inspect reporting on its
target) count toward that other request's history.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.api.proofs import Proven, fresh_observation

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from vs_core.api import Observation
    from vs_runtime.api.core import ExecutionResult


def observations_in(result: ExecutionResult) -> Iterator[Observation]:
    """Every core observation inside one result, including owner events and targets (the shell commits them with the result)."""
    observed = result.observation
    yield observed.observation
    if observed.target is not None:
        yield observed.target.observation
    for event in result.owner_events:
        owner_observation = getattr(event, "observation", None)
        if owner_observation is not None and owner_observation != observed.observation:
            yield owner_observation


def assert_ingress_proofs(result: ExecutionResult) -> None:
    """Core's step ingress refuses a registered-outcome field that lacks its codec proof."""
    observed = result.observation
    for carrier, path in ((observed, "outcome"), (observed.target, "target")):
        if carrier is None:
            continue
        carries = any(
            value is not None
            for value in (
                carrier.outcome,
                carrier.operation_schema,
                carrier.outcome_schema,
                carrier.outcome_json,
            )
        )
        assert not carries or carrier.outcome_is_registered, (
            f"{path} carries an unproven registered outcome that core refuses at ingress"
        )


def assert_core_accepts(results: Sequence[ExecutionResult], *, expect_retry: bool = True) -> None:
    """Require every observation to be proven fresh against its request's earlier ones.

    With ``expect_retry`` the scenario must also have advanced a sequence, so a
    scenario that never retries cannot pass as a contract case.
    """
    histories: dict[str, list[Observation]] = {}
    for result in results:
        assert_ingress_proofs(result)
        for observation in observations_in(result):
            history = histories.setdefault(observation.request_id.root, [])
            verdict = fresh_observation(tuple(history), observation, complete=True)
            assert isinstance(verdict, Proven), (
                f"core rejects observation {observation.event_id.root} "
                f"(sequence {observation.sequence}) after {[row.sequence for row in history]}: "
                f"{verdict}"
            )
            history.append(observation)
    if expect_retry:
        assert any(row.sequence >= 1 for history in histories.values() for row in history), (
            "no request was observed twice with different results: not a retry scenario"
        )
