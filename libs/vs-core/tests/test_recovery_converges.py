"""From any persisted state, recovery converges to the crash-free result.

A toy search (`toy_agent_search`: workspaces, agent turns that wait for evaluations, polled
jobs, adoption) runs on the real core against scripted executors, once without a crash. Then the
same run is replayed with the host dying after generated commit points: every authorization,
every delivery of a request's observations, every clock move. The new host reloads the envelope
through the codec, sends ``RecoveryStarted`` under the next epoch and answers core's inspections
from what the executors really did (sealed result, begun but never sealed, never begun). A
crash number past the end of the crash-free run falls inside a recovery, so double and triple
crashes are generated too.

Whatever the crashes, the run ends with the crash-free run's result, summary and evidence, and
the executors performed the crash-free run's effects, each request's effect once. No asyncio, no
shell: pure core and a scripted world.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.testing import Answer, Faults, Running

from .toy_agent_search import ROLES, Config, ToyHost, run_toy, summary

if TYPE_CHECKING:
    from vs_core.api.testing import Trace


class StableHost(ToyHost):
    """Executors whose behavior is fixed by whom they serve, not by the order they are asked in.

    ``ToyHost`` numbers fresh turns and submitted jobs as they arrive, and a recovery
    legitimately changes the arrival order (a request sent again arrives later), which would
    hand the same turn another script. Here the n-th workstream's turn of a role, and its job,
    behave the same however the run is interleaved.
    """

    def during_turn(
        self, request: core.Request, state: core.CoreState
    ) -> tuple[core.CoreEvent, ...]:
        """The evaluation a fresh turn submits while it runs, chosen by its attempt and role."""
        del state
        if not isinstance(request, core.DispatchTurn) or request.request_id is None:
            return ()
        role = request.turn.session.role_id.root.removeprefix("toy-")
        if role not in self.roles or not self.waits:
            return ()
        ordinal = _workstream(request.scope) * len(ROLES) + ROLES.index(role)
        if not self.waits[ordinal % len(self.waits)]:
            return ()
        self._asking[request.request_id.root] = self.ends[ordinal % len(self.ends)]
        return (self._call(request, role),)

    def _submit(self, request: core.SubmitMeasurement) -> Answer:
        job = f"job:{request.scope.owner.root}"
        self._running[job] = self.polls[_workstream(request.scope) % len(self.polls)]
        return Running(resource_id=core.ResourceId(root=job))


def _workstream(scope: core.Scope) -> int:
    """The number of the workstream (attempt ``w<n>``) a request is in."""
    return int(scope.owner.root.removeprefix("w"))


@dataclass(frozen=True)
class World:
    """One generated run: the workstreams, which turns wait, and how long the jobs poll."""

    workstreams: int
    in_flight: int
    waits: tuple[bool, ...]
    polls: tuple[int, ...]
    roles: tuple[str, ...]

    @functools.cached_property
    def crash_free(self) -> Trace:
        """The run without a crash: what every crashed run must end as."""
        return self.run()

    def run(self, faults: Faults | None = None) -> Trace:
        """Drive the toy search against fresh executors."""
        host = StableHost(waits=self.waits, polls=self.polls, roles=self.roles)
        config = Config(workstreams=self.workstreams, in_flight=self.in_flight)
        return run_toy(host, config, faults=faults)


WORLDS = st.builds(
    World,
    workstreams=st.integers(1, 2),
    in_flight=st.integers(1, 2),
    waits=st.lists(st.booleans(), min_size=1, max_size=4).map(tuple),
    polls=st.lists(st.integers(0, 3), min_size=1, max_size=3).map(tuple),
    roles=st.sampled_from((ROLES, ("judge",), ("implementer",))),
)

PINNED = World(
    workstreams=1, in_flight=1, waits=(True,), polls=(1,), roles=("implementer", "judge")
)

UNSEALED = {
    "sealed": lambda _request: False,
    "unsealed": lambda _request: True,
    "mixed": lambda request: sum(map(ord, str(request.request_id))) % 2 == 0,
}
"""Whether a request executed but not delivered at a crash died before sealing its result."""


def work_of(trace: Trace) -> list[str]:
    """The effects that happened, named by what was asked and not by core's bookkeeping.

    A request's id carries a running number (a recovery's inspections use numbers) and a digest
    of its payload, which can hold the time of an observation: neither says what was done.
    """
    asked = {r.request_id.root: r for r in trace.requests if r.request_id is not None}
    names = []
    for effect in trace.effects:
        request = asked[effect]
        decision = getattr(request, "decision_id", None)
        identity = re.sub(r"^(\w+):\d+:", r"\1:", effect) if decision is None else decision.root
        identity = re.sub(r":observe:\d+$", ":observe", identity)  # which poll it was is pacing
        names.append(f"{request.kind} {request.scope.owner.root} {identity}")
    return sorted(names)


def outcome(trace: Trace) -> dict[str, object]:
    """What a finished run is judged by: its result, its summary and its evidence."""
    finishes = [d for d in trace.decisions if isinstance(d, core.Finish)]
    return {
        "terminal": trace.core.run.status,
        "results": [(f.result.outcome, f.result.reason, f.result.selection) for f in finishes],
        # workstreams end in whatever order they finish; what each did is the summary
        "summary": sorted(summary(trace).done, key=lambda done: done.key),
        "evidence": sorted(item.evidence_id.root for item in trace.core.evaluation.evidence),
    }


# No shrink phase: a crashed run costs ~0.3 s and a failing set of crash numbers shrinks through
# hundreds of them. The pinned examples below are the minimal counterexamples we know; a new
# failure prints its world and crash numbers (see ``note``) unshrunk.
