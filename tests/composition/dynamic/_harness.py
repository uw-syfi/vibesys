"""Scenario harness for the dynamic search on the core path, over the production host.

A scenario runs the way the product does: a CLI-built request goes through
``launch.default_runs`` and the production host composition (real Git worktrees, the
real ``.vibesys`` state store, the runtime's run loop, the trusted evaluation scripts).
The core policy is selected by wiring (``dynamic_core_registration``) until the
switch makes it the built-in. Two things are Fake because they are external:

- the cluster: ``vs_slurm.fake_connector`` in executing mode runs every production job
  script on this host, so a job finishes at its first poll;
- the agents: :class:`ScriptedAgents` answers each turn from a per-role script. A
  core turn is reply-driven: an implementer edits its worktree and replies, the
  framework measures the nominated candidate, and no agent calls an evaluation tool.

The input project's benchmark reports ``throughput = VALUE`` from ``queue.py`` and its
accuracy check raises ``ValueError`` when ``VALUE`` is negative, so an agent's edit
decides every trusted outcome. A candidate that also sets ``REQUIRED`` above ``VALUE``
fails its benchmark the way a warmup cut short does: an ``error`` record whose partial
measurement is ``VALUE`` rounds per second out of ``REQUIRED``.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import launch
from entrypoints.cli import build_run_request, parse_cli_invocation
from launch import LaunchSettings
from vibesys.api import (
    CoreEventType,
    OrchestrationRegistry,
    ResumeRef,
    RunRequest,
    RunResult,
    RunStatus,
    RunStopped,
)
from vibesys.dynamic_core import dynamic_core_registration
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vs_agent.api import NULL_SKILL_SELECTION, AgentCapabilities, SessionScope
from vs_agent.api.testing import FakeAgentClient
from vs_project.api import Project, StoredEnvelope
from vs_slurm.fake_connector import HOLD_FILE, SUBMITTED_FILE, executing_cluster, recorded_commands

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path
    from typing import Any

    from vibesys.api import CoreEvent, RunHandle
    from vs_agent.api import AgentSessionKey, SessionStore, SkillSelection
    from vs_agent.api.testing import FakeInvocation

_PLANNER_SLOTS = re.compile(r"Schedule at most (\d+) ")
_MEMBER = re.compile(
    r"^(?:Own|Review) hypothesis `(?P<id>[^\n]*)` (?:in this isolated|without editing)"
)

_BENCHMARK = """\
import json, pathlib, sys
namespace = {}
exec(pathlib.Path("queue.py").read_text(), namespace)
output = sys.argv[sys.argv.index("--vs-output") + 1] if "--vs-output" in sys.argv else "profile-result.jsonl"
value, required = namespace["VALUE"], namespace.get("REQUIRED")
passed = required is None or value >= required
hello = {"kind": "hello", "protocol": 2, "metrics": {"throughput": {"direction": "max"}}}
outcome = (
    {"kind": "result", "values": {"throughput": float(value)}}
    if passed
    else {
        "kind": "error",
        "message": f"warmup stopped: {value}/{required} rounds",
        "partial": {
            "name": "warmup_rounds_per_s",
            "value": value,
            "direction": "max",
            "unit": "rounds/s",
            "target": required,
            "progress": {"completed": value, "required": required, "unit": "rounds"},
        },
    }
)
pathlib.Path(output).write_text("".join(json.dumps(record) + "\\n" for record in (hello, outcome)))
raise SystemExit(0 if passed else 1)
"""

_ACCURACY = """\
import pathlib
namespace = {}
exec(pathlib.Path("queue.py").read_text(), namespace)


def check(value):
    if value < 0:
        raise ValueError(f"queue depth {value} is negative")


check(namespace["VALUE"])
"""

_AGENT_CONFIG = (
    '[model]\nname = "scripted"\n[evaluation]\n'
    "observe_interval_seconds = 1\nobserve_backoff_cap_seconds = 1\n"
)


class ScriptExhaustedError(AssertionError):
    """An agent turn arrived that the scenario did not script."""


class AgentTransportError(RuntimeError):
    """A scripted agent CLI failure (the process died mid-turn)."""


@dataclass(frozen=True, slots=True)
class Turn:
    """One agent turn as the scripted agent sees it."""

    invocation: FakeInvocation

    @property
    def prompt(self) -> str:
        """Return the user prompt the orchestration sent."""
        return self.invocation.user_prompt

    @property
    def workspace(self) -> Path:
        """Return the agent's working directory (a real worktree)."""
        return self.invocation.workspace

    @property
    def slots(self) -> int:
        """Return how many workstreams a planning prompt asks for."""
        match = _PLANNER_SLOTS.search(self.prompt)
        if match is None:
            message = "planner prompt does not state its free slots"
            raise AssertionError(message)
        return int(match.group(1))

    def set_value(self, value: int) -> None:
        """Edit the candidate, as an implementer does."""
        (self.workspace / "queue.py").write_text(f"VALUE = {value}\n", encoding="utf-8")

    def write_queue(self, text: str) -> None:
        """Replace the candidate's ``queue.py`` with ``text``."""
        (self.workspace / "queue.py").write_text(text, encoding="utf-8")

    def value(self) -> int:
        """Return the candidate's current ``VALUE``."""
        text = (self.workspace / "queue.py").read_text(encoding="utf-8")
        return int(text.split("=", 1)[1].split()[0])


type Reply = Mapping[str, object] | BaseException | Callable[[Turn], Mapping[str, object]]


@dataclass
class ScriptedAgents:
    """Per-role agent scripts behind one Fake client.

    Planner replies are consumed in call order. Implementer and judge replies are keyed
    by hypothesis id, because parallel workstreams interleave. A turn with no scripted
    reply fails the turn and is recorded in ``unscripted``, which every scenario asserts
    is empty.
    """

    planner: deque[Reply] = field(default_factory=deque)
    implementers: dict[str, deque[Reply]] = field(default_factory=lambda: defaultdict(deque))
    judges: dict[str, deque[Reply]] = field(default_factory=lambda: defaultdict(deque))
    unscripted: list[str] = field(default_factory=list)
    turns: list[tuple[str, str | None, str]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _client: FakeAgentClient | None = None
    _members: dict[AgentSessionKey, str] = field(default_factory=dict)

    def plan(self, *replies: Reply) -> ScriptedAgents:
        """Queue planner replies."""
        self.planner.extend(replies)
        return self

    def implement(self, hypothesis_id: str, *replies: Reply) -> ScriptedAgents:
        """Queue implementer replies for one hypothesis."""
        self.implementers[hypothesis_id].extend(replies)
        return self

    def judge(self, hypothesis_id: str, *replies: Reply) -> ScriptedAgents:
        """Queue judge replies for one hypothesis."""
        self.judges[hypothesis_id].extend(replies)
        return self

    def prompts(self, role: str, hypothesis_id: str | None = None) -> list[str]:
        """Return the prompts one role (and hypothesis) received, in order."""
        return [
            prompt
            for kind, member, prompt in self.turns
            if kind == role and (hypothesis_id is None or member == hypothesis_id)
        ]

    def invocations(self, role: str, hypothesis_id: str | None = None) -> list[FakeInvocation]:
        """Return the Fake client's recorded calls, including durable session identity."""
        if self._client is None:
            return []
        return [
            call
            for call in self._client.calls_for(role)
            if hypothesis_id is None or _member(call) == hypothesis_id
        ]

    def client(
        self,
        *,
        session_store: SessionStore | None = None,
        skill_selection: SkillSelection = NULL_SKILL_SELECTION,
        **_kwargs: object,
    ) -> FakeAgentClient:
        """Build a Fake client with the capabilities the agent CLI drivers report."""
        client = FakeAgentClient(
            capabilities=AgentCapabilities(session_reuse=True, provider_session_resume=True),
            session_reuse=True,
            session_store=session_store,
            skill_selection=skill_selection,
        )
        for role in (ORCHESTRATOR.id, IMPLEMENTER.id, JUDGE.id):
            client.set_response(role, self._answer)
        self._client = client
        return client

    def wait_cancelled(self, timeout: float) -> bool:
        """Block a scripted turn until the run cancels its agents' turns."""
        assert self._client is not None
        return self._client.wait_cancelled(timeout)

    def _answer(self, invocation: FakeInvocation) -> dict[str, object]:
        member = _prompt_member(invocation)
        with self._lock:
            if invocation.session_key is not None:
                if member is not None:
                    self._members[invocation.session_key] = member
                else:
                    member = self._members.get(invocation.session_key)
            if member is None:
                member = _member(invocation)
            self.turns.append((invocation.kind, member, invocation.user_prompt))
            queue = self._queue(invocation.kind, member)
            if not queue:
                self.unscripted.append(f"{invocation.kind}:{member}")
                message = f"no scripted reply for {invocation.kind} {member!r}"
                raise ScriptExhaustedError(message)
            reply = queue.popleft()
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, Mapping):
            return dict(reply)
        return dict(reply(Turn(invocation)))

    def _queue(self, kind: str, member: str | None) -> deque[Reply]:
        if kind == ORCHESTRATOR.id:
            return self.planner
        if member is None:
            return deque()
        if kind == IMPLEMENTER.id:
            return self.implementers[member]
        if kind == JUDGE.id:
            return self.judges[member]
        return deque()


def _prompt_member(invocation: FakeInvocation) -> str | None:
    match = _MEMBER.match(invocation.user_prompt)
    return match.group("id") if match is not None else None


def _member(invocation: FakeInvocation) -> str | None:
    prompted = _prompt_member(invocation)
    if prompted is not None:
        return prompted
    key = invocation.session_key
    if key is None:
        return None
    if key.scope is SessionScope.MEMBER:
        role, separator, member = key.identifier.partition(":")
        return member if separator and role == invocation.kind else None
    if key.scope is SessionScope.MEMBER_GENERATION:
        role, member, _generation = json.loads(key.identifier)
        return member if role == invocation.kind else None
    return None


def workstream(
    identifier: str,
    *,
    title: str | None = None,
    task: str | None = None,
    continue_hypothesis: bool = False,
    parent_hypothesis_id: str | None = None,
) -> dict[str, object]:
    """Return one planner implement-workstream entry."""
    return {
        "kind": "implement",
        "hypothesis_id": identifier,
        "title": title or f"Investigate {identifier}"[:80],
        "hypothesis": f"Mechanism {identifier} limits throughput.",
        "task": task or f"Implement and verify {identifier}.",
        "pass_criteria": "Accuracy passes and throughput improves.",
        "continue_hypothesis": continue_hypothesis,
        "parent_hypothesis_id": parent_hypothesis_id,
    }


def portfolio(
    *workstreams: Mapping[str, object], updates: Iterable[Mapping[str, object]] = ()
) -> dict[str, object]:
    """Return one planner reply."""
    return {
        "reasoning": "Independent mechanisms limit throughput.",
        "workstreams": list(workstreams),
        "hypothesis_updates": list(updates),
    }


def implemented(identifier: str, *, outcome: str = "nominated") -> dict[str, object]:
    """Return one implementer reply."""
    return {
        "summary": f"Implemented {identifier}.",
        "outcome": outcome,
        "evidence": [{"location": "queue.py", "purpose": "the change"}],
    }


def edit_to(value: int, identifier: str) -> Callable[[Turn], Mapping[str, object]]:
    """Return an implementer turn that sets ``VALUE`` and nominates the candidate."""

    def turn(agent: Turn) -> dict[str, object]:
        agent.set_value(value)
        return implemented(identifier)

    return turn


PASS = {"passed": True, "analysis": "The change is correct and evidenced.", "feedback": ""}


@dataclass(frozen=True, slots=True)
class LoopInput:
    """An input project and the Fake cluster its run evaluates on."""

    root: Path
    cluster: Path
    slurm_config: Path
    agent_config: Path

    @classmethod
    def create(cls, base: Path, *, poll_interval_s: float = 1.0) -> LoopInput:
        """Write the input project, the executing cluster, and its Slurm config."""
        root = base / "project"
        root.mkdir(parents=True)
        (root / "OBJECTIVE.md").write_text("Raise queue throughput.\n", encoding="utf-8")
        (root / "queue.py").write_text("VALUE = 1\n", encoding="utf-8")
        (root / "benchmark.py").write_text(_BENCHMARK, encoding="utf-8")
        (root / "accuracy.py").write_text(_ACCURACY, encoding="utf-8")
        (root / "vibesys.input.toml").write_text(
            'version = 1\n[agent]\ndomain = "generic"\n'
            '[accuracy]\ncommand = ["python", "accuracy.py"]\ntimeout_seconds = 30\n'
            '[benchmark]\ncommand = ["python", "benchmark.py"]\n'
            "timeout_seconds = 30\nresult_protocol = 2\n",
            encoding="utf-8",
        )
        cluster = executing_cluster(base / "cluster")
        remote = base / "remote"
        remote.mkdir()
        connector = json.dumps([sys.executable, "-m", "vs_slurm.fake_connector", str(cluster)])
        config = base / "slurm.toml"
        config.write_text(
            "[slurm]\n"
            'name = "fake"\n'
            f'remote_workspace_root = "{remote}"\n'
            f"poll_interval_seconds = {poll_interval_s}\n"
            f'transport = {{ kind = "connector", command = {connector} }}\n',
            encoding="utf-8",
        )
        agent_config = base / "agent.toml"
        agent_config.write_text(_AGENT_CONFIG, encoding="utf-8")
        return cls(root, cluster, config, agent_config)

    def hold_jobs(self) -> None:
        """Leave every job submitted from now on pending until it is cancelled."""
        (self.cluster / HOLD_FILE).touch()

    def release_jobs(self) -> None:
        """Run jobs submitted from now on again."""
        (self.cluster / HOLD_FILE).unlink()

    def cluster_commands(self) -> list[str]:
        """Return every command the cluster received."""
        return recorded_commands(self.cluster)

    def sbatch_count(self) -> int:
        """Return how many jobs were submitted to the cluster."""
        return sum("sbatch " in command for command in self.cluster_commands())

    @property
    def submitted(self) -> Path:
        """Return the cluster's pending-job announcement path (create a FIFO there)."""
        return self.cluster / SUBMITTED_FILE

    def request(self, **flags: int | str) -> RunRequest:
        """Build the run request from the command line, as the operator's CLI does.

        ``flags`` are long options without the dashes, underscores for hyphens:
        ``max_rounds=2`` is ``--max-rounds 2``.
        """
        chosen: dict[str, int | str] = {"max_rounds": 1, "max_in_flight": 1, **flags}
        argv = [
            "--outer-loop", "dynamic",
            "--input", str(self.root),
            "--config", str(self.agent_config),
            "--run-environment", "slurm",
            "--slurm-config", str(self.slurm_config),
            "--profiler", "none",
            "--backend", "cpu",
        ]  # fmt: skip
        for name, value in chosen.items():
            argv += [f"--{name.replace('_', '-')}", str(value)]
        return build_run_request(parse_cli_invocation(argv))


@dataclass
class LoopRun:
    """The outcome of one run over a :class:`LoopInput`."""

    run_id: str
    result: RunResult | None
    error: BaseException | None
    events: list[CoreEvent]

    @property
    def succeeded(self) -> bool | None:
        """Return whether the run reported success; None when it raised."""
        return self.result.succeeded if self.result is not None else None

    @property
    def status(self) -> RunStatus | None:
        """Return the run's reported status; None when it raised."""
        return self.result.status if self.result is not None else None

    def notes(self) -> list[str]:
        """Return the framework warnings the run published."""
        return [
            str(getattr(event.data, "summary", ""))
            for event in self.events
            if event.type is CoreEventType.FRAMEWORK_WARNING
        ]


def run_request(
    request: RunRequest,
    agents: ScriptedAgents,
    *,
    on_handle: Callable[[RunHandle], None] | None = None,
) -> LoopRun:
    """Execute a built request through the production host composition."""
    registry = OrchestrationRegistry()
    registry.register(dynamic_core_registration())
    runs = launch.default_runs(
        LaunchSettings(registry=registry, agent_client_factory=agents.client)
    )
    events: list[CoreEvent] = []

    async def collect(handle: RunHandle) -> None:
        events.extend([event async for event in handle.events()])

    async def run() -> LoopRun:
        handle = runs.resume(request) if request.resume is not None else runs.start(request)
        collector = asyncio.ensure_future(collect(handle))
        if on_handle is not None:
            on_handle(handle)
        try:
            result = await handle.result()
        # lint-waiver: LW-601001 [BLE001]; crash scenarios assert on the run's own
        # > failure together with its events. pytest.raises at each call site would
        # > lose the events and run id the assertions need, and naming one type would
        # > couple the harness to how the host wraps a failure.
        except (Exception, RunStopped) as error:  # noqa: BLE001
            await asyncio.gather(collector, return_exceptions=True)
            return LoopRun(handle.run_id, None, error, events)
        await asyncio.gather(collector, return_exceptions=True)
        return LoopRun(result.run_id, result, None, events)

    return asyncio.run(run())


def resume_request(request: RunRequest, run_id: str) -> RunRequest:
    """Return ``request`` as a resume of ``run_id``."""
    return request.model_copy(update={"resume": ResumeRef(run_id=run_id), "exp_name": run_id})


class CoreRecords:
    """A finished run's committed core record, read through the state store."""

    def __init__(self, loop_input: LoopInput, run_id: str) -> None:
        """Load the run's committed envelope; fail when none was committed."""
        self.loop_input = loop_input
        self.run_id = run_id
        self.project = Project.open(loop_input.root)
        stored = self.project.state_store(run_id).load()
        assert isinstance(stored, StoredEnvelope)
        self.envelope: dict[str, Any] = json.loads(stored.payload)["envelope"]

    @property
    def core(self) -> dict[str, Any]:
        """Return the core's committed run state."""
        return self.envelope["core"]

    @property
    def strategy(self) -> dict[str, Any]:
        """Return the strategy's committed state."""
        return self.envelope["strategy"]

    @property
    def run(self) -> dict[str, Any]:
        """Return the core run record (status, facts, result)."""
        return self.core["run"]

    @property
    def outcome(self) -> tuple[str, str | None]:
        """Return the run's (status, result outcome)."""
        result = self.run.get("result")
        return self.run["status"], (result or {}).get("outcome")

    @property
    def selection(self) -> dict[str, Any] | None:
        """Return the run's selected result, if any."""
        return (self.run.get("result") or {}).get("selection")

    def baseline_metric(self) -> float | None:
        """Return the trusted baseline's first metric value, if it was measured."""
        baseline = self.strategy["baseline"]
        return baseline["metrics"][0]["value"] if baseline["metrics"] else None

    def attempt(self, hypothesis_id: str, sequence: int | None = None) -> dict[str, Any]:
        """Return the strategy's record of one attempt (the last of the hypothesis by default)."""
        rows = [
            row
            for row in self.strategy["attempts"]
            if row["plan"]["work_id"] == hypothesis_id
            and (sequence is None or row["sequence"] == sequence)
        ]
        assert rows, f"no attempt of {hypothesis_id!r}"
        return rows[-1]

    def publications(self) -> bytes:
        """Return the run's journaled publications."""
        return self.project.state.portable_namespace(self.run_id, "publications").read_bytes(
            "publications.json"
        )


def logs_dir(loop_input: LoopInput, run_id: str) -> Path:
    """Return a run's logs directory."""
    found = sorted(loop_input.root.glob(f".vibesys/state/**/runs/{run_id}/logs"))
    assert found, f"no logs for {run_id}"
    return found[0]
