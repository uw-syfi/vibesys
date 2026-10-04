"""Whole-loop harness for the dynamic plugin over the production layers.

A scenario runs the dynamic plugin through the public product session
(``vibesys.api.testing.create_session``) with the built-in registry, the real
compute backend, the real ``.vibesys`` state store and Git worktrees, the
evaluation agent service, and the Slurm run environment. Only two things are
fake, because they are external:

- the cluster: ``vs_slurm.fake_connector`` in executing mode runs every
  production job script on this host, so a job is finished at its first poll;
- the agents: :class:`ScriptedAgents` answers each turn from a per-role script.
  An implementer turn edits its worktree and calls the real evaluation MCP
  tools over the run's evaluation socket, as an agent CLI would.

The input project's benchmark reports ``throughput = VALUE`` from ``queue.py``
(protocol 2), and its accuracy check raises ``ValueError`` when ``VALUE`` is
negative, so an agent's edit decides every trusted outcome. A candidate that
also sets ``REQUIRED`` above ``VALUE`` fails its benchmark the way a warmup cut
short does: an ``error`` record whose partial measurement is ``VALUE`` rounds
per second out of ``REQUIRED``. A candidate that sets ``WARMUP_STOPS`` fails
it the way the qwen3.5-9b-mi210 benchmark does: that bundle's own harness code
replays a recorded ``session_runner`` stderr (``golden/warmup_stop.stderr``,
r19's stopped warmup) and writes its error record.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import threading
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from launch import built_in_orchestrations
from launch.testing import FakeStopTimer, create_session
from vibesys.api import (
    ComputeBackend,
    Config,
    OrchestrationDescriptor,
    ResumeRef,
    RunRequest,
    RunStopped,
)
from vibesys.events import CoreEventType
from vibesys.inputs import load_input_bundle
from vibesys.orchestration.dynamic import PLUGIN, DynamicOptions
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR, PROFILER
from vibesys.orchestration.dynamic.models import DynamicState
from vibesys.orchestration.profilers import ProfilerKind
from vs_agent.api import AgentCapabilities
from vs_agent.api.testing import FakeAgentClient
from vs_evaluation.api import EvaluationAgentRole
from vs_evaluation.api.tools import build_evaluation_tools
from vs_project.api import Project
from vs_runtime.api.infrastructure import RunEnvironmentSpec
from vs_sandbox.api import create_compute_backend
from vs_slurm.fake_connector import (
    HOLD_FILE,
    SUBMITTED_FILE,
    executing_cluster,
    recorded_commands,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from vibesys.events import CoreEvent
    from vs_agent.api import AgentClientProtocol
    from vs_agent.api.testing import FakeInvocation

# A deadlock guard for the evaluation tools: each evaluation finishes within a
# few seconds, and raising the bound never turns a failure into a pass.
_AWAIT_S = 120.0
_PLANNER_SLOTS = re.compile(r"Schedule at most (\d+) ")
_MEMBER = re.compile(
    r"^(?:Own|Review) hypothesis `(?P<id>[^\n]*)` (?:in this isolated|without editing)"
)

_REPO = Path(__file__).resolve().parents[5]
# The real benchmark harness of the bundle whose warmup stops r19 recorded.
BUNDLE_BENCHMARK = _REPO / "examples/model-serving/qwen3.5-9b-mi210/benchmark/run.py"
WARMUP_STOP_STDERR = Path(__file__).with_name("golden") / "warmup_stop.stderr"

_BENCHMARK = """\
import importlib.util, json, pathlib, sys
namespace = {{}}
exec(pathlib.Path("queue.py").read_text(), namespace)
output = sys.argv[sys.argv.index("--vs-output") + 1] if "--vs-output" in sys.argv else "profile-result.jsonl"
if namespace.get("WARMUP_STOPS"):
    spec = importlib.util.spec_from_file_location("bundle_benchmark", {bundle!r})
    bundle = sys.modules["bundle_benchmark"] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bundle)
    report = bundle.ProtocolReport(pathlib.Path(output))
    watch = bundle.WarmupWatch(
        bundle.WARMUP_TIMEOUT_S, bundle.WARMUP_SESSION_CEILING_TOK_S, label="warmup sub-run"
    )
    try:
        bundle.run_session_runner(
            pathlib.Path({engine!r}),
            [],
            timeout_s=bundle.WARMUP_TIMEOUT_S,
            label="warmup sub-run",
            watch=watch.feed,
        )
    except bundle.HarnessError as error:
        report.fail(str(error), error.partial)
        raise SystemExit(1)
    raise SystemExit("the recorded warmup did not stop")
value, required = namespace["VALUE"], namespace.get("REQUIRED")
passed = required is None or value >= required
hello = {{"kind": "hello", "protocol": 2, "metrics": {{"throughput": {{"direction": "max"}}}}}}
outcome = (
    {{"kind": "result", "values": {{"throughput": float(value)}}}}
    if passed
    else {{
        "kind": "error",
        "message": f"warmup stopped: {{value}}/{{required}} rounds",
        "partial": {{
            "name": "warmup_rounds_per_s",
            "value": value,
            "direction": "max",
            "unit": "rounds/s",
            "target": required,
            "progress": {{"completed": value, "required": required, "unit": "rounds"}},
        }},
    }}
)
pathlib.Path(output).write_text("".join(json.dumps(record) + "\\n" for record in (hello, outcome)))
raise SystemExit(0 if passed else 1)
"""

# The GPU node's profiler, faked like the cluster: the remote interpreter runs
# every command with this host's Python, except the profiler's trusted capture,
# which it answers as ``remote_capture.py --print-output`` does: one trace
# directory under the requested profile store and the capture summary on stdout.
# The real capture runtime owns start/readiness/load/stop. Only GPU tracing is fake.
_REMOTE_PYTHON = """\
#!{python}
import json
import os
import pathlib
import sys

if sys.argv[1] == "rocprof_profiler/remote_capture.py":
    sys.path.insert(0, "profilers_common")
    import capture_runtime

    request = json.loads(sys.argv[sys.argv.index("--request-json") + 1])
    profiles = pathlib.Path(sys.argv[sys.argv.index("--profiles") + 1])
    lifecycle = capture_runtime.Lifecycle(**request["lifecycle"])
    captured = capture_runtime.run_capture(
        [], lifecycle, kind="timeline", out_dir=profiles / "timeline-1", meta={{}}
    )
    failure = capture_runtime.workload_failure(profiles, [captured.capture_id])
    if failure is not None:
        print(captured.load_log_tail)
        print(failure)
        raise SystemExit(1)
    (captured.out_dir / "stats.csv").write_text("kernel,share\\nqueue_step,0.75\\n")
    print("Timeline: queue_step holds 75% of device time.")
    raise SystemExit(0)
os.execv("{python}", ["{python}", *sys.argv[1:]])
"""

_SERVICE = (
    "import pathlib, signal, sys, threading; "
    "signal.signal(signal.SIGINT, lambda *_: sys.exit(0)); "
    "pathlib.Path(sys.argv[1], f'service-ready-{sys.argv[2]}').touch(); "
    "threading.Event().wait()"
)

_ACCURACY = """\
import pathlib
namespace = {}
exec(pathlib.Path("queue.py").read_text(), namespace)


def check(value):
    if value < 0:
        raise ValueError(f"queue depth {value} is negative")


check(namespace["VALUE"])
"""


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

    def value(self) -> int:
        """Return the candidate's current ``VALUE``."""
        text = (self.workspace / "queue.py").read_text(encoding="utf-8")
        return int(text.split("=", 1)[1])

    def evaluate(self, *kinds: str) -> dict[str, object]:
        """Submit an evaluation through the real MCP tools and wait for its result."""
        handle = self.submit(*kinds)
        while True:
            reply = self._call("await_evaluation", {"handle_id": handle, "timeout_s": _AWAIT_S})
            result = reply["result"]
            assert isinstance(result, dict)
            if result["outcome"] != "running":
                return reply

    def await_once(self, handle: str, timeout_s: float) -> dict[str, object]:
        """Make one await_evaluation call and return its result, running or final."""
        result = self._call("await_evaluation", {"handle_id": handle, "timeout_s": timeout_s})[
            "result"
        ]
        assert isinstance(result, dict)
        return result

    def trusted_operations(self) -> dict[str, object]:
        """Read the run's trusted operations through the planner's real MCP tool."""
        return self._call("trusted_operations", {})

    def accepted_evidence(self, *kinds: str) -> list[dict[str, object]]:
        """Return the trusted evidence already recorded for this turn's exact candidate."""
        evidence = self._call("accepted_evidence", {"evidence_kinds": kinds})["evidence"]
        assert isinstance(evidence, list)
        return evidence

    def submit(self, *kinds: str) -> str:
        """Submit an evaluation without waiting; return its handle."""
        return str(self.submit_reply(*kinds)["handle_id"])

    def submit_reply(self, *kinds: str) -> dict[str, object]:
        """Submit an evaluation without waiting; return the tool's whole reply."""
        return self._call("submit_evaluation", {"evidence_kinds": kinds})

    def _call(self, name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        servers = self.invocation.tool_servers or []
        server = next(item for item in servers if item.name == "vs-evaluation")
        env = dict(server.env)
        tools = build_evaluation_tools(
            socket_path=Path(env["VS_EVALUATION_SOCKET"]),
            token=env["VS_EVALUATION_TOKEN"],
            role=EvaluationAgentRole(env["VS_EVALUATION_ROLE"]),
            profiler_available=env.get("VS_EVALUATION_PROFILER_AVAILABLE") == "1",
            run_observer=env.get("VS_EVALUATION_RUN_OBSERVER") == "1",
        )
        tool = next(item for item in tools if item.name == name)
        reply = json.loads(tool.handler(tool.input_schema.model_validate(arguments)))
        assert isinstance(reply, dict)
        return reply


type Reply = Mapping[str, object] | BaseException | Callable[[Turn], Mapping[str, object]]


@dataclass
class ScriptedAgents:
    """Per-role agent scripts behind one production-capable Fake client.

    Planner replies are consumed in call order. Implementer and judge replies
    are keyed by hypothesis id, because parallel workstreams interleave. A
    turn with no scripted reply fails the turn and is recorded in
    ``unscripted``, which every scenario asserts is empty.
    """

    planner: deque[Reply] = field(default_factory=deque)
    implementers: dict[str, deque[Reply]] = field(default_factory=lambda: defaultdict(deque))
    judges: dict[str, deque[Reply]] = field(default_factory=lambda: defaultdict(deque))
    # Profiler turns, in call order: a profiler session is keyed by an
    # operation's conversation, not by a hypothesis.
    profilers: deque[Reply] = field(default_factory=deque)
    unscripted: list[str] = field(default_factory=list)
    turns: list[tuple[str, str | None, str]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _client: FakeAgentClient | None = None

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

    def profile(self, *replies: Reply) -> ScriptedAgents:
        """Queue profiler replies."""
        self.profilers.extend(replies)
        return self

    def prompts(self, role: str, hypothesis_id: str | None = None) -> list[str]:
        """Return the prompts one role (and hypothesis) received, in order."""
        return [
            prompt
            for kind, member, prompt in self.turns
            if kind == role and (hypothesis_id is None or member == hypothesis_id)
        ]

    def client(self) -> FakeAgentClient:
        """Build a Fake client with the capabilities the agent CLI drivers report."""
        client = FakeAgentClient(
            capabilities=AgentCapabilities(
                tool_servers=True, session_reuse=True, provider_session_resume=True
            ),
            session_reuse=True,
        )
        for role in (ORCHESTRATOR.id, IMPLEMENTER.id, JUDGE.id, PROFILER.id):
            client.set_response(role, self._answer)
        self._client = client
        return client

    def wait_cancelled(self, timeout: float) -> bool:
        """Block a scripted turn until the run cancels its agents' turns."""
        assert self._client is not None
        return self._client.wait_cancelled(timeout)

    def _answer(self, invocation: FakeInvocation) -> dict[str, object]:
        member = _member(invocation)
        with self._lock:
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
        if kind == PROFILER.id:
            return self.profilers
        if member is None:
            return deque()
        if kind == IMPLEMENTER.id:
            return self.implementers[member]
        if kind == JUDGE.id:
            return self.judges[member]
        return deque()


def planner_history(prompt: str) -> dict[str, dict[str, object]]:
    """Return the history rows of one planning prompt, keyed by hypothesis or profile id."""
    _, rest = prompt.split("## Compact hypothesis history\n", 1)
    rows = json.loads(rest.split("\n", 1)[0])
    assert isinstance(rows, list)
    return {
        str(row["profile_id"] if row.get("kind") == "profile" else row["hypothesis_id"]): row
        for row in rows
    }


def _member(invocation: FakeInvocation) -> str | None:
    match = _MEMBER.match(invocation.user_prompt)
    return match.group("id") if match is not None else None


def workstream(
    identifier: str,
    *,
    title: str | None = None,
    task: str | None = None,
    continue_hypothesis: bool = False,
) -> dict[str, object]:
    """Return one planner workstream entry."""
    return {
        "hypothesis_id": identifier,
        "title": title or f"Investigate {identifier}"[:80],
        "hypothesis": f"Mechanism {identifier} limits throughput.",
        "task": task or f"Implement and verify {identifier}.",
        "pass_criteria": "Accuracy passes and throughput improves.",
        "continue_hypothesis": continue_hypothesis,
    }


def profile_workstream(
    identifier: str, target: str | None, question: str = "Where does the time go?"
) -> dict[str, object]:
    """Return one planner profile workstream entry."""
    return {
        "kind": "profile",
        "profile_id": identifier,
        "target_hypothesis_id": target,
        "question": question,
        "decision_impact": "Prioritize the implementation that removes the dominant cost.",
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


def edit_to(
    value: int, identifier: str, *evaluations: tuple[str, ...]
) -> Callable[[Turn], Mapping[str, object]]:
    """Return an implementer turn that sets ``VALUE`` and runs ``evaluations`` first."""

    def turn(agent: Turn) -> dict[str, object]:
        agent.set_value(value)
        for kinds in evaluations:
            agent.evaluate(*kinds)
        return implemented(identifier)

    return turn


PASS = {"passed": True, "analysis": "The change is correct and evidenced."}


@dataclass(frozen=True, slots=True)
class LoopInput:
    """An input project and the Fake cluster its run evaluates on."""

    root: Path
    cluster: Path
    slurm_config: Path
    profiler: ProfilerKind = ProfilerKind.NONE
    backend: ComputeBackend = ComputeBackend.CPU

    @classmethod
    def create(
        cls,
        base: Path,
        *,
        profiled: bool = False,
        serviced: bool | None = None,
        connector: Callable[[list[str]], list[str]] | None = None,
        poll_interval_s: float = 3600.0,
    ) -> LoopInput:
        """Write the input project, the executing cluster, and its Slurm config.

        A ``profiled`` input is an LLM-serving project on ROCm, so its run
        provisions the rocprof profiler agent, the production profiling target.
        A ``serviced`` input (by default, a profiled one) configures a service
        for each job, so the GPU node's profiler captures under load and the
        run's evaluation executor produces trusted profile evidence; without
        it, the executor cannot. With a service, the benchmark also gets the
        production arguments that reach it through the job's port placeholder,
        as the MI210 cluster's policy does.
        ``connector`` wraps the Fake cluster's connector command (a fault
        injector does). A run whose cluster answers a poll wrongly polls again
        after ``poll_interval_s``.
        """
        domain = "llm-serving" if profiled else "generic"
        root = base / "project"
        root.mkdir(parents=True)
        (root / "OBJECTIVE.md").write_text("Raise queue throughput.\n", encoding="utf-8")
        (root / "queue.py").write_text("VALUE = 1\n", encoding="utf-8")
        # Stands in for session_runner: prints the recorded stderr, as the
        # binary did up to the harness's stop.
        engine = base / "recorded-session-runner"
        engine.write_text(f"#!/bin/sh\nexec cat {WARMUP_STOP_STDERR} >&2\n", encoding="utf-8")
        engine.chmod(0o755)
        (root / "benchmark.py").write_text(
            _BENCHMARK.format(bundle=str(BUNDLE_BENCHMARK), engine=str(engine)), encoding="utf-8"
        )
        (root / "accuracy.py").write_text(_ACCURACY, encoding="utf-8")
        (root / "profile.py").write_text(
            "import pathlib\nnamespace = {}\nexec(pathlib.Path('queue.py').read_text(), namespace)\n"
            "if not namespace.get('SERVING', True):\n    raise ConnectionError('server never served')\n"
            "print('fixed profiling load completed')\n",
            encoding="utf-8",
        )
        (root / "vibesys.input.toml").write_text(
            f'version = 1\n[agent]\ndomain = "{domain}"\n'
            '[accuracy]\ncommand = ["python", "accuracy.py"]\n'
            '[benchmark]\ncommand = ["python", "benchmark.py"]\nresult_protocol = 2\n'
            '[profile]\ncommand = ["python", "profile.py"]\n',
            encoding="utf-8",
        )
        cluster = executing_cluster(base / "cluster")
        remote = base / "remote"
        remote.mkdir()
        command = [sys.executable, "-m", "vs_slurm.fake_connector", str(cluster)]
        connector_json = json.dumps(connector(command) if connector is not None else command)
        config = base / "slurm.toml"
        # A one-hour poll interval: a job that is not finished at its first
        # poll stalls the test visibly instead of being waited for.
        remote_python = base / "remote-python"
        remote_python.write_text(
            _REMOTE_PYTHON.format(python=sys.executable),
            encoding="utf-8",
        )
        remote_python.chmod(0o755)
        # The service only announces readiness through a file of its own, so
        # concurrent tests never contend for the port the job derives.
        service_argv = ["python", "-c", _SERVICE, str(base), "VIBESYS_DYNAMIC_PORT"]
        service = (
            "[vibesys.service]\n"
            f"command = {json.dumps(service_argv)}\n"
            f'readiness_url = "file://{base}/service-ready-VIBESYS_DYNAMIC_PORT"\n'
            "startup_timeout_seconds = 60\n"
            if (profiled if serviced is None else serviced)
            else ""
        )
        # As production configures it: the benchmark reaches the job's service
        # through the port the job script substitutes.
        benchmark_arguments = (
            'benchmark_arguments = ["--base-url", "http://127.0.0.1:VIBESYS_DYNAMIC_PORT/v1"]\n'
            if service
            else ""
        )
        config.write_text(
            "[slurm]\n"
            'name = "fake"\n'
            f'remote_workspace_root = "{remote}"\n'
            f"poll_interval_seconds = {poll_interval_s}\n"
            f'transport = {{ kind = "connector", command = {connector_json} }}\n'
            "[vibesys]\n"
            f'remote_python = "{remote_python}"\n' + benchmark_arguments + service,
            encoding="utf-8",
        )
        if profiled:
            return cls(root, cluster, config, ProfilerKind.ROCPROF, ComputeBackend.ROCM)
        return cls(root, cluster, config)

    def fail_profile_workloads(self) -> None:
        """Make the candidate completion endpoint fail even though its health check works."""
        (self.root / "queue.py").write_text("VALUE = 1\nSERVING = False\n", encoding="utf-8")

    def hold_jobs(self) -> None:
        """Leave every job submitted from now on pending until it is cancelled."""
        (self.cluster / HOLD_FILE).touch()

    def release_jobs(self) -> None:
        """Run jobs submitted from now on again."""
        (self.cluster / HOLD_FILE).unlink()

    def cluster_commands(self) -> list[str]:
        """Return every command the cluster received."""
        return recorded_commands(self.cluster)

    @property
    def submitted(self) -> Path:
        """Return the cluster's pending-job announcement path (create a FIFO there)."""
        return self.cluster / SUBMITTED_FILE


@dataclass
class LoopRun:
    """The outcome of one session over a :class:`LoopInput`."""

    run_id: str
    succeeded: bool | None
    error: BaseException | None
    events: list[CoreEvent]

    def notes(self) -> list[str]:
        """Return the framework warnings the run published."""
        return [
            str(getattr(event.data, "summary", ""))
            for event in self.events
            if event.type is CoreEventType.FRAMEWORK_WARNING
        ]


def options(**changes: object) -> DynamicOptions:
    """Return a small dynamic configuration."""
    return DynamicOptions.model_validate(
        {
            "interface": "service",
            "max_rounds": 1,
            "max_retries_per_round": 2,
            "judge_every": 1,
            "official_eval_every": 1,
            "max_in_flight": 1,
            "metric_space": {"objectives": [{"name": "throughput", "direction": "max"}]},
            **changes,
        }
    )


class AgentsSource(Protocol):
    """Anything that builds the run's agent client (scripted or generated agents)."""

    def client(self) -> AgentClientProtocol:
        """Return the client every agent turn of the run goes through."""
        ...


# lint-waiver: LW-122303 [PLR0913]; scenarios pass only the hooks they use, by
# > keyword. A scenario-options object would add a type every scenario builds
# > for one or two hooks, and positional loop_input/agents/options stay explicit.
def run_loop(  # noqa: PLR0913
    loop_input: LoopInput,
    agents: AgentsSource,
    configured: DynamicOptions,
    *,
    resume_run_id: str | None = None,
    on_session: Callable[[object], None] | None = None,
    stop_timer: FakeStopTimer | None = None,
) -> LoopRun:
    """Run the dynamic plugin to its end through the product session."""
    bundle = load_input_bundle(loop_input.root)
    request = RunRequest(
        project_root=loop_input.root,
        orchestration=OrchestrationDescriptor(
            id=PLUGIN.id,
            config_version=PLUGIN.config_version,
            options=configured.model_dump(mode="json"),
        ),
        config=Config.model_validate({"model": {"name": "dynamic-loop"}}),
        input_bundle=bundle,
        objective=bundle.objective,
        exp_name=resume_run_id or "dynamic-loop",
        resume=ResumeRef(run_id=resume_run_id) if resume_run_id else None,
        agent_backend="cli",
        cli_provider="claude",
        profiler_kind=loop_input.profiler,
        backend=loop_input.backend,
        run_environment=RunEnvironmentSpec("slurm", {"config_path": str(loop_input.slurm_config)}),
    )
    events: list[CoreEvent] = []

    def sink(event: CoreEvent) -> None:
        events.append(event)

    client = agents.client()

    async def run() -> LoopRun:
        session = create_session(
            request,
            sink=sink,
            registry=built_in_orchestrations(),
            agent_client_factory=lambda **_kwargs: client,
            backend_factory=create_compute_backend,
            stop_timer=stop_timer or FakeStopTimer(),
        )
        if on_session is not None:
            on_session(session)
        try:
            session.start()
            result = await session.await_result()
        # lint-waiver: LW-140003 [BLE001]; crash scenarios assert on the run's
        # > own failure together with its events. pytest.raises at each call site
        # > would lose the events and run id the assertions need, and naming one
        # > type would couple the harness to how the host wraps a plugin failure.
        except (Exception, RunStopped) as error:  # noqa: BLE001
            # A stopped run ends with the typed ``RunStopped``, a BaseException.
            return LoopRun(_run_id(events), None, error, events)
        finally:
            session.close()
        return LoopRun(result.run_id, result.succeeded, None, events)

    return asyncio.run(run())


def _run_id(events: list[CoreEvent]) -> str:
    return next(event.run_id for event in events if event.run_id)


def load_state(loop_input: LoopInput, run_id: str) -> DynamicState:
    """Load the plugin state through the store's strict JSON load."""
    state = (
        Project.open(loop_input.root)
        .state.portable_namespace(run_id, PLUGIN.id)
        .slot("state.json", DynamicState)
        .load_optional()
    )
    assert state is not None
    return state


def state_path(loop_input: LoopInput, run_id: str) -> Path:
    """Return the plugin state file of one run."""
    return loop_input.root / ".vibesys" / "state" / "runs" / run_id / PLUGIN.id / "state.json"


def commit_as_schema_v4(loop_input: LoopInput, run_id: str) -> None:
    """Rewrite and commit a run's state as dynamic schema version 4 wrote it.

    Version 5 added ``implementer_started``; version 6 dropped
    ``validation_recipe_artifact`` from implementations. The run commits its
    state to the project's Git history, so an older VibeSys left it committed.
    """
    path = state_path(loop_input, run_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema_version"] = 4
    for item in data["workstreams"]:
        item.pop("implementer_started")
        if item["implementation"] is not None:
            item["implementation"]["validation_recipe_artifact"] = None
    path.write_text(json.dumps(data), encoding="utf-8")
    for command in (
        ("git", "add", "--force", "--", str(path)),
        (
            "git",
            *("-c", "user.name=vibesys", "-c", "user.email=vibesys@localhost"),
            *("commit", "--quiet", "-m", "dynamic: state written by schema version 4"),
        ),
    ):
        # lint-waiver: LW-140004 [S603]; a fixed argv commits the fixture the way
        # > an older VibeSys did. Committing through vs-project would write the
        # > current schema, which is exactly what this fixture must avoid.
        subprocess.run(command, cwd=loop_input.root, check=True, capture_output=True)  # noqa: S603
