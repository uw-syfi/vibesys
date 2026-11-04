"""A scripted run in its own process, for a test to pause, kill and resume.

``run_process`` is the entry of a process a test starts through ``multiprocessing``'s fork
server, which imports this module once and forks every run process from that image. A run
process is a real process in its own session, as a shell-started ``vibesys`` is, and costs a
fork instead of the 3.6 s a fresh interpreter takes to import the product.

It runs the scripted scenario of ``LoopInput.create(BASE)`` through ``run_request``, the
production host composition over the product's on-disk ``.vibesys`` state, and reports on a
pipe, one dict per message: each run event as ``{"event": ..., "stage": ...}``,
``{"held": AT:KIND}`` when the hold is reached, and last ``{"outcome": ...}``. ``resume``
relaunches the saved run as a new epoch, as ``vibesys --resume`` does.

``hold`` stops the first turn of the agent ``KIND``, once its session is open and the agent is
working, until the test sends on the release pipe, so the test places an interruption inside
that turn without a sleep.
"""

from __future__ import annotations

import asyncio
import faulthandler
import os
import signal
import sys
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel
from tests.e2e.dynamic_loop_harness import LoopInput, resume_request, run_request
from tests.e2e.scripted_value_run import scripted_setup

from vibesys.events import CoreEventType, WinnerSelectedData
from vs_agent.api import ScriptedAgentClient
from vs_sim.api import OsThreads

if TYPE_CHECKING:
    from collections.abc import Callable
    from multiprocessing.connection import Connection

    from vibesys.api import CoreEvent, RunHandle
    from vs_agent.api import (
        AgentClientProtocol,
        AgentEventSink,
        AgentSpec,
        ScriptedTurn,
    )
    from vs_agent.fake_client import ResponseSource, ResponseValue

ROUNDS = 1
TURN = "turn"


@dataclass(frozen=True)
class Hold:
    """The first turn of one agent kind, held at one point of its life."""

    at: str
    kind: str

    @classmethod
    def parse(cls, text: str) -> Hold:
        at, _, kind = text.partition(":")
        if at != TURN or not kind:
            message = f"a hold is {TURN}:KIND, not {text!r}"
            raise ValueError(message)
        return cls(at, kind)


class Channel:
    """A run process's pipes to the test: reports out, releases in."""

    def __init__(self, reports: Connection, releases: Connection) -> None:
        self._reports = reports
        self._releases = releases
        self._lock = threading.Lock()

    def say(self, **fields: object) -> None:
        """Send one report; the turn threads and the event reader all report."""
        with self._lock:
            self._reports.send(fields)

    def wait_for_release(self) -> None:
        """Block until the test lets the held turn go on."""
        self._releases.recv()


class _Gate:
    """Stops one turn until the parent releases it, once."""

    def __init__(self, hold: Hold, channel: Channel) -> None:
        self._hold = hold
        self._channel = channel
        self._lock = threading.Lock()
        self._spent = False

    def reach(self, at: str, kind: str) -> None:
        with self._lock:
            if self._spent or (at, kind) != (self._hold.at, self._hold.kind):
                return
            self._spent = True
        self._channel.say(held=f"{at}:{kind}")
        self._channel.wait_for_release()


def _held_client_factory(hold: Hold | None, channel: Channel) -> Callable[..., AgentClientProtocol]:
    gate = None if hold is None else _Gate(hold, channel)

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
        return ScriptedAgentClient(replace(setup, responders=responders), threads=OsThreads())

    return build


def _held_responder(gate: _Gate, kind: str, responder: ResponseSource) -> ResponseSource:
    def answer(turn: ScriptedTurn) -> ResponseValue:
        gate.reach(TURN, kind)
        return responder if isinstance(responder, BaseModel | dict) else responder(turn)

    return answer


def _report(event: CoreEvent, channel: Channel) -> None:
    data = event.data
    channel.say(
        event=event.type.value,
        stage=getattr(data, "stage", None) or getattr(data, "phase", None),
        gate=getattr(getattr(data, "gate", None), "value", None),
        span=getattr(getattr(data, "span", None), "id", None),
    )


async def _report_all(handle: RunHandle, channel: Channel) -> None:
    channel.say(run_id=handle.run_id)
    async for event in handle.events():
        _report(event, channel)


def main(base: Path, resume: str | None, hold: Hold | None, channel: Channel) -> None:
    """Run the scenario under ``base`` and report its outcome."""
    loop_input = LoopInput.attach(base)
    request = loop_input.request(agent_backend="scripted", max_rounds=ROUNDS, max_in_flight=1)
    if resume is not None:
        request = resume_request(request, resume)
    reporters: list[asyncio.Task[None]] = []
    run = run_request(
        request,
        client_factory=_held_client_factory(hold, channel),
        slurm_processes=loop_input.connector,
        relaunch_saved=resume is not None,
        on_handle=lambda handle: reporters.append(
            asyncio.ensure_future(_report_all(handle, channel))
        ),
    )
    run.raise_error()
    assert run.result is not None
    selections = [
        event.data.selection
        for event in run.events
        if event.type is CoreEventType.WINNER_SELECTED
        and isinstance(event.data, WinnerSelectedData)
    ]
    channel.say(
        outcome={
            "run_id": run.result.run_id,
            "status": run.result.status.value,
            "failure": None if run.result.failure is None else str(run.result.failure),
            "selections": selections,
        }
    )


@dataclass(frozen=True)
class RunSpec:
    """What one run process is asked to do."""

    base: str
    """The project the scenario was created under."""
    resume: str | None
    """The run to relaunch as a new epoch; ``None`` starts a new run."""
    hold: str | None
    """``AT:KIND`` of the turn to hold, or ``None``."""
    errors: str
    """The file the process's stderr goes to."""
    environment: dict[str, str]
    """The test's environment when it asked: each case has its own state home, which the fork
    server's image (taken when its first case started) does not."""
    cwd: str
    """The test's working directory when it asked."""


def run_process(spec: RunSpec, reports: Connection, releases: Connection) -> None:
    """The run process: its own session, and its stderr in ``spec.errors``.

    The fork server ends a process without shutting its interpreter down, so the imported product
    is not freed on the way out (1.5 to 3 s a process). A failure leaves its traceback in
    ``spec.errors`` and a nonzero status, which the test reports. ``SIGUSR1`` writes every
    thread's stack to ``spec.errors``: a test that finds the run silent asks for it before it fails,
    so a stall says where the run waits.
    """
    os.setsid()
    os.environ.clear()
    os.environ.update(spec.environment)
    os.chdir(spec.cwd)
    os.dup2(os.open(spec.errors, os.O_WRONLY | os.O_CREAT | os.O_TRUNC), sys.stderr.fileno())
    faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)
    held = None if spec.hold is None else Hold.parse(spec.hold)
    main(Path(spec.base), spec.resume, held, Channel(reports, releases))
