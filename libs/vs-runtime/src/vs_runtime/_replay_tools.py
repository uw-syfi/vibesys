"""The native ``TurnYields``: host-authenticated tool obligations over the writer, without CoreState."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_proto.api import replay as pb

if TYPE_CHECKING:
    from vs_core.api.replay import (
        ReplayToolPending,
        ReplayToolReady,
        ReplayToolRefused,
        SuspensionRefusal,
    )
    from vs_runtime._replay_runtime import ReplayRuntime
    from vs_sim.api import Clock


type ReplayToolLookup = ReplayToolPending | ReplayToolReady | ReplayToolRefused


@dataclass(frozen=True)
class ReplayTurnYields:
    """``TurnYields`` keyed by physical turn span, with typed admission and lookup.

    The wiring supplies each ``turn`` from its dispatched span, never agent
    arguments. Producers are already-durable evaluation starts supplied by the
    evaluation owner. A retry of the same tool call retains the same owed reply
    identity. A wait goes through the one suspension gate and is refused with its
    typed reason instead of being recorded.
    """

    runtime: ReplayRuntime
    clock: Clock

    def yielded(self, turn: str, /) -> pb.ReplayTurnWait | None:
        return self.runtime.archive.dependencies.yielded(turn)

    @staticmethod
    def reply_id(turn: str, call_id: str) -> str:
        return (
            "tool/"
            + hashlib.sha256(
                pb.ReplayToolAcknowledgement(turn=turn, reply_id=call_id).to_binary()
            ).hexdigest()
        )

    def _record(
        self, fact: pb.ReplayToolSubmission | pb.ReplayToolAcknowledgement | pb.ReplayTurnWait
    ) -> SuspensionRefusal | None:
        now = self.clock.now()
        seconds = int(now)
        return self.runtime.tool_fact(
            fact, now=now, at=Timestamp(seconds=seconds, nanos=int((now - seconds) * 1_000_000_000))
        )

    def submit(self, turn: str, call_id: str, producer: str) -> ReplayToolLookup:
        self._record(
            pb.ReplayToolSubmission(
                turn=turn, call_id=call_id, reply_id=self.reply_id(turn, call_id), producer=producer
            )
        )
        return self.lookup(turn, call_id)

    def lookup(self, turn: str, call_id: str) -> ReplayToolLookup:
        archive = self.runtime.archive
        return archive.dependencies.lookup(turn, self.reply_id(turn, call_id), archive.span)

    def acknowledge(self, turn: str, call_id: str) -> None:
        self._record(pb.ReplayToolAcknowledgement(turn=turn, reply_id=self.reply_id(turn, call_id)))

    def wait(self, turn: str, *call_ids: str) -> pb.ReplayTurnWait | SuspensionRefusal:
        fact = pb.ReplayTurnWait(
            turn=turn, reply_ids=[self.reply_id(turn, key) for key in call_ids]
        )
        refusal = self._record(fact)
        return fact if refusal is None else refusal
