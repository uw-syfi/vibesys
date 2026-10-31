"""A scripted run in its own process, for a test to pause, kill and resume.

``python -m tests.e2e._interruptible_run BASE [--resume RUN_ID] [--hold AT:KIND]`` runs the
scripted scenario of ``LoopInput.create(BASE)`` through ``run_request``, the production host
composition over the product's on-disk ``.vibesys`` state, and reports on stdout, one JSON
object per line: each run event as ``{"event": ..., "stage": ...}``, ``{"held": AT:KIND}`` when
the hold is reached, and last ``{"outcome": ...}``. ``--resume`` relaunches the saved run as
a new epoch, as ``vibesys --resume`` does.

``--hold`` stops the first turn of the agent ``KIND`` until a line arrives on stdin, so the
parent places an interruption inside that turn without a sleep: ``start`` holds the turn before
its agent session exists, ``turn`` holds it once the session is open and the agent is working.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel
from tests.e2e.dynamic_loop_harness import LoopInput, resume_request, run_request
from tests.e2e.scripted_value_run import NoAgents, scripted_setup

from vibesys.events import CoreEventType, WinnerSelectedData
from vs_agent.api import ScriptedAgentClient
from vs_sim.api import OsThreads

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.api import CoreEvent, RunHandle
    from vs_agent.api import (
        AgentClientProtocol,
        AgentEventSink,
        AgentObserver,
        AgentSessionKey,
        AgentSessionSpec,
        AgentSpec,
        AgentTurnRequest,
        AgentTurnResult,
        ScriptedClientSetup,
        ScriptedTurn,
    )
    from vs_agent.fake_client import ResponseSource, ResponseValue
    from vs_sim.api import Threads

ROUNDS = 1
LEASE_SECONDS = 2.0
"""A killed writer's lease: production's is 60 s, which a resume after a kill would wait out."""
START = "start"
TURN = "turn"


@dataclass(frozen=True)
class Hold:
    """The first turn of one agent kind, held at one point of its life."""

    at: str
    kind: str

    @classmethod
    def parse(cls, text: str) -> Hold:
        at, _, kind = text.partition(":")
        if at not in {START, TURN} or not kind:
            message = f"a hold is {START}:KIND or {TURN}:KIND, not {text!r}"
            raise ValueError(message)
        return cls(at, kind)


def _say(**fields: object) -> None:
    sys.stdout.write(json.dumps(fields) + "\n")
    sys.stdout.flush()


class _Gate:
    """Stops one turn until the parent releases it, once."""

    def __init__(self, hold: Hold) -> None:
        self._hold = hold
        self._lock = threading.Lock()
        self._spent = False

    def reach(self, at: str, kind: str) -> None:
        with self._lock:
            if self._spent or (at, kind) != (self._hold.at, self._hold.kind):
                return
            self._spent = True
        _say(held=f"{at}:{kind}")
        sys.stdin.readline()


class _HeldClient(ScriptedAgentClient):
    """A scripted client whose turns reach the gate before their agent session starts."""

    def __init__(self, gate: _Gate, setup: ScriptedClientSetup, *, threads: Threads) -> None:
        super().__init__(setup, threads=threads)
        self._gate = gate

    def run(
        self,
        *,
        session_spec: AgentSessionSpec,
        turn: AgentTurnRequest,
        session_key: AgentSessionKey | None = None,
        observer: AgentObserver | None = None,
    ) -> AgentTurnResult:
        self._gate.reach(START, session_spec.role)
        return super().run(
            session_spec=session_spec, turn=turn, session_key=session_key, observer=observer
        )


def _held_client_factory(
    hold: Hold | None,
) -> Callable[..., AgentClientProtocol]:
    gate = None if hold is None else _Gate(hold)

    def build(
        *, spec: AgentSpec, events: AgentEventSink, log_dir: Path | None = None, **_services: object
    ) -> AgentClientProtocol:
        setup = scripted_setup(spec=spec, events=events, log_dir=log_dir)
        if gate is None:
            return ScriptedAgentClient(setup, threads=OsThreads())
        responders = {
            kind: _held_responder(gate, kind, responder)
            for kind, responder in setup.responders.items()
        }
        return _HeldClient(gate, replace(setup, responders=responders), threads=OsThreads())

    return build


def _held_responder(gate: _Gate, kind: str, responder: ResponseSource) -> ResponseSource:
    def answer(turn: ScriptedTurn) -> ResponseValue:
        gate.reach(TURN, kind)
        return responder if isinstance(responder, BaseModel | dict) else responder(turn)

    return answer


def _report(event: CoreEvent) -> None:
    data = event.data
    _say(
        event=event.type.value,
        stage=getattr(data, "stage", None) or getattr(data, "phase", None),
        gate=getattr(getattr(data, "gate", None), "value", None),
    )


async def _report_all(handle: RunHandle) -> None:
    _say(run_id=handle.run_id)
    async for event in handle.events():
        _report(event)


def main(base: Path, resume: str | None, hold: Hold | None) -> None:
    """Run the scenario under ``base`` and report its outcome."""
    loop_input = LoopInput.attach(base)
    request = loop_input.request(agent_backend="scripted", max_rounds=ROUNDS, max_in_flight=1)
    if resume is not None:
        request = resume_request(request, resume)
    reporters: list[asyncio.Task[None]] = []
    run = run_request(
        request,
        NoAgents(),
        client_factory=_held_client_factory(hold),
        slurm_processes=loop_input.connector,
        verify=False,
        relaunch_saved=resume is not None,
        lease_seconds=LEASE_SECONDS,
        on_handle=lambda handle: reporters.append(asyncio.ensure_future(_report_all(handle))),
    )
    run.raise_error()
    assert run.result is not None
    selections = [
        event.data.selection
        for event in run.events
        if event.type is CoreEventType.WINNER_SELECTED
        and isinstance(event.data, WinnerSelectedData)
    ]
    _say(
        outcome={
            "run_id": run.result.run_id,
            "status": run.result.status.value,
            "failure": None if run.result.failure is None else str(run.result.failure),
            "selections": selections,
        }
    )


if __name__ == "__main__":
    arguments = sys.argv[1:]
    held = arguments[arguments.index("--hold") + 1] if "--hold" in arguments else None
    resumed = arguments[arguments.index("--resume") + 1] if "--resume" in arguments else None
    main(Path(arguments[0]), resumed, None if held is None else Hold.parse(held))
