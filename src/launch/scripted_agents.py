"""The shell of the scripted agents: the workspace they read and edit, and the turns they fail.

The decisions are `vibesys.orchestration.dynamic.scripted` (pure). Here each turn's workspace is read
for the facts a decision needs, the edit it returns is written back, what the agent says is sent to the
run's event sink, and a turn that is to fail raises what a failing agent raises.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.scripted import api as scripted
from vs_agent.api import AgentOutputSchemaError, SessionScope

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from vs_agent.api import AgentEventSink, ScriptedTurn


def _reader(root: Path) -> scripted.Reader:
    def read(relative: str) -> str | None:
        path = root / relative
        return path.read_text(encoding="utf-8") if path.is_file() else None

    return read


def _member(turn: ScriptedTurn) -> str:
    """The hypothesis the turn is about: named in its prompt, else by its conversation."""
    named = scripted.named_in(turn.user_prompt)
    if named is not None:
        return named
    key = turn.session_key
    if key is not None and key.scope is SessionScope.MEMBER:
        role, separator, member = key.identifier.partition(":")
        if separator and role == turn.kind:
            return member
    if key is not None and key.scope is SessionScope.MEMBER_GENERATION:
        role, member, _generation = json.loads(key.identifier)
        if role == turn.kind:
            return str(member)
    message = f"the {turn.kind} turn names no hypothesis"
    raise AgentOutputSchemaError(message)


def _wire(turn: ScriptedTurn, payload: dict[str, object]) -> dict[str, object]:
    """``payload`` as the turn's reply type reads it: inside the envelope of a role that may wait."""
    reply_type = turn.response_cls
    if reply_type is not None and "reply" in reply_type.model_fields:
        return {"reply": payload}
    return payload


class ScriptedDynamicAgents:
    """The responders of one run's scripted planner, implementer and judge."""

    def __init__(
        self,
        *,
        seed: int,
        sink: AgentEventSink,
        workloads: tuple[scripted.Workload, ...] = scripted.WORKLOADS,
    ) -> None:
        """Script agents over ``workloads`` that narrate their turns to ``sink``."""
        self._seed = seed
        self._sink = sink
        self._workloads = workloads

    def responders(self) -> Mapping[str, Callable[[ScriptedTurn], dict[str, object]]]:
        """The responder of each role a scripted dynamic run asks, keyed by the role's agent kind.

        The profiler, the merger and the outliner have none: a plan that asks for them, which the
        scripted planner never does, fails its turn as an agent whose reply the schema refuses.
        """
        return {
            scripted.PLANNER_KIND: self._plan,
            scripted.IMPLEMENTER_KIND: self._implement,
            scripted.JUDGE_KIND: self._judge,
        }

    def _plan(self, turn: ScriptedTurn) -> dict[str, object]:
        workload = scripted.recognized(_reader(turn.workspace), self._workloads)
        return self._answer(turn, scripted.plan(self._seed, workload, turn.user_prompt))

    def _implement(self, turn: ScriptedTurn) -> dict[str, object]:
        read = _reader(turn.workspace)
        workload = scripted.recognized(read, self._workloads)
        source = read(workload.source) if workload is not None else None
        result = scripted.implement(self._seed, workload, _member(turn), source)
        if result.edited_source is not None and workload is not None:
            (turn.workspace / workload.source).write_text(result.edited_source, encoding="utf-8")
        return self._answer(turn, result)

    def _judge(self, turn: ScriptedTurn) -> dict[str, object]:
        read = _reader(turn.workspace)
        workload = scripted.recognized(read, self._workloads)
        source = read(workload.source) if workload is not None else None
        return self._answer(turn, scripted.judge(self._seed, workload, _member(turn), source))

    def _answer(self, turn: ScriptedTurn, result: scripted.Turn) -> dict[str, object]:
        for line in result.narration:
            self._sink.agent_output(
                f"{line}\n",
                channel="assistant",
                agent_kind=turn.kind,
                round_label=turn.round_label,
                invocation_id=turn.invocation_id,
            )
        if result.failure is scripted.Fault.BAD_REPLY:
            message = "the reply did not match the reply schema"
            raise AgentOutputSchemaError(message)
        if result.failure is scripted.Fault.LOST_TURN:
            message = "the agent process ended before it replied"
            raise ConnectionError(message)
        if result.reply is None:
            message = "a scripted turn ended with neither a reply nor a failure"
            raise AssertionError(message)
        return _wire(turn, result.reply)
