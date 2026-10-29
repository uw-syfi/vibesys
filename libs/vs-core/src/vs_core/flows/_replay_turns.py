"""Turn bounds and tool dependencies derived from the committed native prefix."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from protobuf.wkt.google.protobuf.any_pb import Any

from vs_core._continuations import SuspensionRefusal, suspension_gate
from vs_core._wait_clock import held_turn_bound
from vs_core.types.records import CapacityHold
from vs_proto.api import replay as pb
from vs_proto.api import replay_agent, replay_evaluation, validate

from ._replay_sources import ReplayError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from protobuf import Message

    from vs_proto.api import Record, Span


def _check_metadata(value: Message) -> None:
    validate(value)
    if value.to_binary() != value.to_binary(write_unknown_fields=False):
        problem = f"unknown replay dependency metadata fields in {value.desc().type_name}"
        raise ReplayError(problem)


def agent_turn(span: Span) -> replay_agent.ReplayAgentRequest:
    operation = span.replay
    request = None if operation is None or operation.policy is None else operation.policy.request
    if request is None or request.type_url != replay_agent.ReplayAgentRequest.desc().type_name:
        problem = "tool dependency requires an authenticated agent turn"
        raise ReplayError(problem)
    native = replay_agent.ReplayAgentRequest.from_binary(request.value)
    _check_metadata(native)
    if native.action is None or native.action.field != "turn":
        problem = "tool dependency requires an authenticated agent turn"
        raise ReplayError(problem)
    return native


def effective_deadline(bound: pb.ReplayTurnBound) -> float:
    """The stated limit plus its credited hold, by the positional turn-bound rule.

    A native run has no run deadline, so the credited bound is never capped.
    """
    # Native turns record only ended holds' credit; no hold is current, so no budget applies.
    hold = CapacityHold(credit=bound.hold_credit_seconds, budget=0.0)
    return held_turn_bound(bound.deadline, hold, None).at


def _continues(span: Span) -> bool:
    operation = span.replay
    request = None if operation is None or operation.policy is None else operation.policy.request
    if request is None or request.type_url != replay_agent.ReplayAgentRequest.desc().type_name:
        return False
    native = replay_agent.ReplayAgentRequest.from_binary(request.value)
    return (
        native.action is not None
        and native.action.field == "turn"
        and native.action.value.continuation_of is not None
    )


def validate_turn_bound(span: Span, lookup: Callable[[str], Span]) -> None:
    operation = span.replay
    if operation is None:
        return
    if operation.turn_bound is None:
        if _continues(span):
            problem = "continuation requires its original recorded turn bound"
            raise ReplayError(problem)
        return
    bound = operation.turn_bound
    native = agent_turn(span)
    turn = native.action
    if turn is None or turn.field != "turn":
        problem = "turn bound requires a retained turn request"
        raise ReplayError(problem)
    if turn.value.continuation_of:
        origin = lookup(turn.value.continuation_of)
        before = agent_turn(origin)
        old = before.action
        if (
            origin.replay is None
            or origin.replay.outcome is None
            or origin.replay.turn_bound != bound
            or span.parent != bound.origin
            or native.scope != before.scope
            or old is None
            or old.field != "turn"
            or turn.value.session is None
            or old.value.session is None
            or turn.value.session.id != old.value.session.id
            or turn.value.session.role != old.value.session.role
            or turn.value.session.access != old.value.session.access
        ):
            problem = "continuation changed its original turn ownership or immutable bound"
            raise ReplayError(problem)
    elif bound.origin != span.id or bound.hold_credit_seconds != turn.value.hold_credit_seconds:
        problem = "original turn bound must name its logical start and recorded hold credit"
        raise ReplayError(problem)


def validate_turn_observation(
    child: Span, parent: Span, yielded: Callable[[str], pb.ReplayTurnWait | None]
) -> None:
    """A bound turn's observation names its own bound and exactly its durable wait."""
    bound = None if parent.replay is None else parent.replay.turn_bound
    observation = child.replay_observation
    if bound is None or observation is None:
        return
    turn = observation.turn
    if turn is None or turn.bound != bound:
        problem = "turn observation must name its immutable recorded bound"
        raise ReplayError(problem)
    if turn.suspension != (yielded(child.id) if turn.succeeded else None):
        problem = "turn observation differs from its durable yield"
        raise ReplayError(problem)


class ReplayToolRefusal(StrEnum):
    UNKNOWN_REPLY = "unknown_reply"
    WRONG_TURN = "wrong_turn"
    PRODUCER_WITHDRAWN = "producer_withdrawn"


@dataclass(frozen=True)
class ReplayToolRefused:
    reason: ReplayToolRefusal


@dataclass(frozen=True)
class ReplayToolPending:
    producer: str


@dataclass(frozen=True)
class ReplayToolReady:
    producer: str
    outcome_bytes: bytes

    def outcome(self) -> Any:
        return Any.from_binary(self.outcome_bytes)


@dataclass(frozen=True)
class ReplayTurnDependencies:
    """Submission and yield obligations, never a saved continuation queue.

    Producers are logical starts in the same scope. Acknowledgement cannot erase
    the dependency. Every wait references this exact physical turn's durable
    submission; a lost reply stays owed across epochs while its turn lives.
    """

    _submissions: dict[str, bytes] = field(default_factory=dict, repr=False)
    _calls: dict[tuple[str, str], str] = field(default_factory=dict, repr=False)
    _acknowledged: frozenset[str] = frozenset()
    _waits: dict[str, bytes] = field(default_factory=dict, repr=False)

    @classmethod
    def from_records(cls, records: Iterable[Record]) -> ReplayTurnDependencies:
        spans: dict[str, Span] = {}
        state = cls()
        for record in records:
            body = record.body
            if body is not None and body.field == "span" and body.value.span is not None:
                spans[body.value.span.id] = body.value.span
            elif body is not None and body.field == "notice" and body.value.detail is not None:
                state = state.record(body.value.detail, spans.__getitem__)
        return state

    def record(self, detail: Any, lookup: Callable[[str], Span]) -> ReplayTurnDependencies:
        kinds = (pb.ReplayToolSubmission, pb.ReplayToolAcknowledgement, pb.ReplayTurnWait)
        name = detail.type_url.rsplit("/", 1)[-1]
        kind = next((k for k in kinds if k.desc().type_name == name), None)
        if kind is None:
            return self
        fact = kind.from_binary(detail.value)
        _check_metadata(fact)
        if isinstance(fact, pb.ReplayToolSubmission):
            return self._submit(fact, lookup)
        if isinstance(fact, pb.ReplayToolAcknowledgement):
            submission = self.submission(fact.reply_id)
            if submission is None or submission.turn != fact.turn:
                problem = "tool acknowledgement has no authenticated owed reply"
                raise ReplayError(problem)
            return ReplayTurnDependencies(
                self._submissions, self._calls, self._acknowledged | {fact.reply_id}, self._waits
            )
        return self._wait(fact, lookup)

    def submission(self, reply_id: str) -> pb.ReplayToolSubmission | None:
        payload = self._submissions.get(reply_id)
        return None if payload is None else pb.ReplayToolSubmission.from_binary(payload)

    def owed(self, lookup: Callable[[str], Span]) -> tuple[pb.ReplayToolSubmission, ...]:
        """Replies still owed to a live physical turn.

        A reply to a turn that ended is settled by that ending: a replacement starts a
        fresh conversation from retained content and never receives it, and the
        producer's outcome stays reachable through ``lookup``. Only the live turn's
        owner acknowledges, so no reply is owed forever.
        """
        return tuple(
            submission
            for key, payload in self._submissions.items()
            if key not in self._acknowledged
            for submission in (pb.ReplayToolSubmission.from_binary(payload),)
            if _live(lookup(submission.turn))
        )

    def yielded(self, turn: str) -> pb.ReplayTurnWait | None:
        value = self._waits.get(turn)
        return None if value is None else pb.ReplayTurnWait.from_binary(value)

    def lookup(
        self, turn: str, reply_id: str, lookup: Callable[[str], Span]
    ) -> ReplayToolReady | ReplayToolPending | ReplayToolRefused:
        submission = self.submission(reply_id)
        if submission is None:
            return ReplayToolRefused(ReplayToolRefusal.UNKNOWN_REPLY)
        if submission.turn != turn:
            return ReplayToolRefused(ReplayToolRefusal.WRONG_TURN)
        producer = lookup(submission.producer)
        if producer.replay is not None and producer.replay.outcome is not None:
            return ReplayToolReady(producer.id, producer.replay.outcome.to_binary())
        if producer.has_field("ended_at"):
            return ReplayToolRefused(ReplayToolRefusal.PRODUCER_WITHDRAWN)
        return ReplayToolPending(producer.id)

    def _submit(
        self, fact: pb.ReplayToolSubmission, lookup: Callable[[str], Span]
    ) -> ReplayTurnDependencies:
        prior = self._submissions.get(fact.reply_id)
        if prior == fact.to_binary():
            return self
        turn, producer = lookup(fact.turn), lookup(fact.producer)
        native = agent_turn(turn)
        operation = producer.replay
        request = (
            None if operation is None or operation.policy is None else operation.policy.request
        )
        if (
            not _live(turn)
            or turn.replay_observation is not None
            or fact.turn in self._waits
            or prior is not None
            or (fact.turn, fact.call_id) in self._calls
            or producer.replay_physical is not None
            or request is None
            or request.type_url != replay_evaluation.ReplayEvaluationRequest.desc().type_name
        ):
            problem = "tool submission conflicts with its authenticated turn or producer"
            raise ReplayError(problem)
        evaluation = replay_evaluation.ReplayEvaluationRequest.from_binary(request.value)
        _check_metadata(evaluation)
        if (
            evaluation.scope != native.scope
            or evaluation.action is None
            or evaluation.action.field not in {"submit", "observe", "collect"}
        ):
            problem = "tool producer must own an evaluation in the requesting turn's scope"
            raise ReplayError(problem)
        return ReplayTurnDependencies(
            {**self._submissions, fact.reply_id: fact.to_binary()},
            {**self._calls, (fact.turn, fact.call_id): fact.reply_id},
            self._acknowledged,
            self._waits,
        )

    def refusal(
        self, fact: pb.ReplayTurnWait, lookup: Callable[[str], Span], *, now: float | None
    ) -> SuspensionRefusal | None:
        """Why ``fact`` cannot suspend its turn, by the one suspension gate.

        The log cannot know the writer's clock, so a recorded wait is checked with
        ``now`` None; the writer checks the time left before recording it. A wait is
        capped at its turn's effective bound and claims its scope while a reply is pending.
        """
        turn = lookup(fact.turn)
        scope = agent_turn(turn).scope
        bound = None if turn.replay is None else turn.replay.turn_bound

        def claimed() -> bool:
            return any(
                other != fact.turn
                and agent_turn(lookup(other)).scope == scope
                and any(
                    isinstance(self.lookup(other, reply, lookup), ReplayToolPending)
                    for reply in pb.ReplayTurnWait.from_binary(payload).reply_ids
                )
                for other, payload in self._waits.items()
            )

        return suspension_gate(
            active=lambda: _live(turn),
            claimed=claimed,
            owned=lambda: (
                not any(
                    isinstance(self.lookup(fact.turn, reply, lookup), ReplayToolRefused)
                    for reply in fact.reply_ids
                )
            ),
            remains=None
            if now is None or bound is None
            else lambda: now < effective_deadline(bound),
        )

    def _wait(
        self, fact: pb.ReplayTurnWait, lookup: Callable[[str], Span]
    ) -> ReplayTurnDependencies:
        prior = self._waits.get(fact.turn)
        if prior == fact.to_binary():
            return self
        if prior is not None:
            problem = "turn wait changed its durable dependencies"
            raise ReplayError(problem)
        refusal = self.refusal(fact, lookup, now=None)
        if refusal is not None:
            problem = f"turn wait was refused by the suspension gate: {refusal.value}"
            raise ReplayError(problem)
        return ReplayTurnDependencies(
            self._submissions,
            self._calls,
            self._acknowledged,
            {**self._waits, fact.turn: fact.to_binary()},
        )


def _live(turn: Span) -> bool:
    physical = turn.replay_physical
    return (
        physical is not None
        and not turn.has_field("ended_at")
        and not physical.has_field("withdrawn_sequence")
    )
