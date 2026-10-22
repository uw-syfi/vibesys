"""Iteration admission and usage are derived from native records, never snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_proto.api import Change, Record, Span, replay, validate
from vs_proto.api import core_iterations as units

from ._replay_resources import ReplayResourceAccounting, ReplayResourcesBlocked
from ._replay_sources import ADMIT_ITERATION, COMPLETE_ITERATION, ReplayError

if TYPE_CHECKING:
    from collections.abc import Iterator


def iteration_outcome(
    operation: replay.OperationReplay,
) -> units.IterationGranted | units.IterationCompleted | None:
    """Classify only exact framework contracts; payload and key must agree."""
    policy, key = operation.policy, operation.key
    if policy is None or policy.request is None or key is None:
        return None
    name = policy.request.type_url.rsplit("/", 1)[-1]
    if name == units.IterationRequest.desc().type_name:
        request = units.IterationRequest.from_binary(policy.request.value)
        validate(request)
        if key.operation != ADMIT_ITERATION:
            problem = "iteration admission request requires the framework admission key"
            raise ReplayError(problem)
        outcome = units.IterationGranted(unit=request, admission_digest=operation.identity_digest)
    elif name == units.IterationCompletion.desc().type_name:
        request = units.IterationCompletion.from_binary(policy.request.value)
        validate(request)
        if key.operation != COMPLETE_ITERATION:
            problem = "iteration completion request requires the framework completion key"
            raise ReplayError(problem)
        outcome = units.IterationCompleted(grant=request.grant)
    else:
        if key.operation in {ADMIT_ITERATION, COMPLETE_ITERATION}:
            problem = "framework iteration key requires its declared typed request"
            raise ReplayError(problem)
        return None
    if policy.reply_type != outcome.desc().type_name:
        problem = "iteration request has an incompatible reply contract"
        raise ReplayError(problem)
    return outcome


@dataclass(frozen=True)
class ReplayIterationBlocked:
    """The next unit needs an ordinary Resume with fresh allowance."""


@dataclass(frozen=True)
class ReplayAccounting:
    """One projection of actual observations and completed logical units.

    Repeated presentation updates and logical acceptance cannot charge an
    observation twice. Earlier-epoch admissions may finish after Resume without
    consuming the fresh epoch's allowance for additional units.
    """

    lifetime_completed: int
    fresh_completed: int
    fresh_pending: int
    paid_operations: int
    unreported_cost_operations: int
    resources: ReplayResourceAccounting
    _grants: tuple[bytes, ...]
    _completed: frozenset[bytes]

    @property
    def reported_cost_usd(self) -> float:
        return self.resources.reported.money_usd

    @property
    def epoch_reported_cost_usd(self) -> float:
        return self.resources.epoch_reported.money_usd

    def decide(
        self, operation: replay.OperationReplay, allowance: replay.ReplayAllowance
    ) -> (
        units.IterationGranted
        | units.IterationCompleted
        | ReplayIterationBlocked
        | ReplayResourcesBlocked
        | None
    ):
        outcome = self.recover(operation)
        if (
            isinstance(outcome, units.IterationGranted)
            and self.fresh_completed + self.fresh_pending >= allowance.iterations
        ):
            return ReplayIterationBlocked()
        return self.resources.decide(operation, allowance.resources) if outcome is None else outcome

    def recover(
        self, operation: replay.OperationReplay
    ) -> units.IterationGranted | units.IterationCompleted | None:
        """A durable framework start already owns admission, regardless of fresh policy."""
        outcome = iteration_outcome(operation)
        if isinstance(outcome, units.IterationCompleted):
            grant = outcome.grant
            if grant is None or grant.to_binary() not in self._grants:
                problem = "iteration completion has no matching admitted unit"
                raise ReplayError(problem)
            if grant.admission_digest in self._completed:
                problem = "iteration unit is already completed"
                raise ReplayError(problem)
        return outcome

    @classmethod
    def from_records(cls, records: tuple[Record, ...], *, epoch: int) -> ReplayAccounting:
        admitted: dict[bytes, tuple[bytes, int]] = {}
        completed: set[bytes] = set()
        fresh_completed = 0
        for expected, started_epoch in _accepted_units(records):
            if isinstance(expected, units.IterationGranted):
                admitted[expected.admission_digest] = (expected.to_binary(), started_epoch)
            else:
                grant = expected.grant
                if (
                    grant is None
                    or admitted.get(grant.admission_digest, (None,))[0] != grant.to_binary()
                ):
                    problem = "iteration completion has no matching admitted unit"
                    raise ReplayError(problem)
                if grant.admission_digest not in completed:
                    completed.add(grant.admission_digest)
                    fresh_completed += int(admitted[grant.admission_digest][1] == epoch)
        pending = sum(
            admitted_epoch == epoch and digest not in completed
            for digest, (_, admitted_epoch) in admitted.items()
        )
        paid, unknown_cost = _paid_observations(records)
        return cls(
            lifetime_completed=len(completed),
            fresh_completed=fresh_completed,
            fresh_pending=pending,
            paid_operations=paid,
            unreported_cost_operations=unknown_cost,
            resources=ReplayResourceAccounting.from_records(records, epoch=epoch),
            _grants=tuple(value for value, _ in admitted.values()),
            _completed=frozenset(completed),
        )


def _spans(records: tuple[Record, ...]) -> Iterator[tuple[Record, Span]]:
    for record in records:
        if record.body is not None and record.body.field == "span":
            span = record.body.value.span
            if span is not None and span.replay is not None:
                yield record, span


def _accepted_units(
    records: tuple[Record, ...],
) -> Iterator[tuple[units.IterationGranted | units.IterationCompleted, int]]:
    started_epochs: dict[str, int] = {}
    for record, span in _spans(records):
        if (
            record.body is not None
            and record.body.field == "span"
            and record.body.value.change == Change.STARTED
        ):
            started_epochs[span.id] = record.epoch
        operation = span.replay
        if (
            operation is None
            or operation.outcome is None
            or operation.accepted_sequence != record.seq
        ):
            continue
        expected = iteration_outcome(operation)
        if expected is None:
            continue
        actual = operation.outcome
        if (
            actual.type_url.rsplit("/", 1)[-1] != expected.desc().type_name
            or actual.value != expected.to_binary()
        ):
            problem = "iteration outcome contradicts its admission proof"
            raise ReplayError(problem)
        yield expected, started_epochs[span.id]


def _paid_observations(records: tuple[Record, ...]) -> tuple[int, int]:
    seen: set[str] = set()
    paid = unknown = 0
    for _, span in _spans(records):
        observation = span.replay_observation
        if observation is None or span.id in seen:
            continue
        seen.add(span.id)
        paid += int(observation.paid)
        unknown += int(observation.paid and not observation.has_field("cost_usd"))
    return paid, unknown
