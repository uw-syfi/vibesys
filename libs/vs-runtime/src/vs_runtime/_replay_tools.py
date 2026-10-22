"""Host-authenticated tool obligations over the native writer, without CoreState."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_proto.api import replay as pb

if TYPE_CHECKING:
    from vs_core.api.replay import ReplayToolPending, ReplayToolReady, ReplayToolRefused
    from vs_runtime._replay_runtime import ReplayRuntime
    from vs_sim.api import Clock


type ReplayToolLookup = ReplayToolPending | ReplayToolReady | ReplayToolRefused


@dataclass(frozen=True)
class ReplayTurnTools:
    """Bind tools to the physical turn authenticated by the session owner.

    The wiring supplies ``turn`` from its dispatched span, never agent arguments.
    Producers are already-durable evaluation starts supplied by the evaluation
    owner. A retry of the same tool call retains the same owed reply identity.
    """

    runtime: ReplayRuntime
    turn: str
    clock: Clock

    def reply_id(self, call_id: str) -> str:
        return (
            "tool/"
            + hashlib.sha256(
                pb.ReplayToolAcknowledgement(turn=self.turn, reply_id=call_id).to_binary()
            ).hexdigest()
        )

    def _record(
        self, fact: pb.ReplayToolSubmission | pb.ReplayToolAcknowledgement | pb.ReplayTurnWait
    ) -> None:
        now = self.clock.now()
        seconds = int(now)
        self.runtime.tool_fact(
            fact, now=now, at=Timestamp(seconds=seconds, nanos=int((now - seconds) * 1_000_000_000))
        )

    def submit(self, call_id: str, producer: str) -> ReplayToolLookup:
        self._record(
            pb.ReplayToolSubmission(
                turn=self.turn, call_id=call_id, reply_id=self.reply_id(call_id), producer=producer
            )
        )
        return self.lookup(call_id)

    def lookup(self, call_id: str) -> ReplayToolLookup:
        archive = self.runtime.archive
        return archive.dependencies.lookup(self.turn, self.reply_id(call_id), archive.span)

    def acknowledge(self, call_id: str) -> None:
        self._record(pb.ReplayToolAcknowledgement(turn=self.turn, reply_id=self.reply_id(call_id)))

    def wait(self, *call_ids: str) -> pb.ReplayTurnWait:
        fact = pb.ReplayTurnWait(turn=self.turn, reply_ids=[self.reply_id(key) for key in call_ids])
        self._record(fact)
        return fact

    def yielded(self) -> pb.ReplayTurnWait | None:
        return self.runtime.archive.dependencies.yielded(self.turn)
