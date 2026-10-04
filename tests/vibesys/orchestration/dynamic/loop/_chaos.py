"""Chaos runs of the dynamic loop: generated agents and a seeded fault plan.

One seed decides everything a run varies: the fault plan (``vs_faults``), every
agent reply (generated from the schema each turn declares), and each agent's
actions (edits to the candidate and tool calls with generated arguments). The
loop and everything under it are production code over the whole-loop harness
(:mod:`._harness`); the Fake cluster's connector runs behind the plan's
cluster wrapper. After the run, the shared loop invariants
(``tests.support.loop_invariants``) and the chaos-only ones below must hold.
"""

from __future__ import annotations

import json
import os
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError
from tests.support.loop_invariants import Invariant, RunRecords, Violation, check
from tests.vibesys.orchestration.dynamic.loop._harness import (
    LoopInput,
    LoopRun,
    load_state,
    options,
    run_loop,
    state_path,
)

from vibesys.api import RunStopped
from vibesys.orchestration.dynamic import DynamicPlanningError
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR, PROFILER
from vs_agent.api import NULL_SKILL_SELECTION, AgentCapabilities, AgentOutputSchemaError
from vs_agent.api.testing import FakeAgentClient
from vs_evaluation.api import EvaluationAgentRole
from vs_evaluation.api.tools import EvaluationServiceClientError, build_evaluation_tools
from vs_faults.api import (
    AgentCrashError,
    Boundary,
    FaultPlan,
    FaultyAgentClient,
    FaultyToolDispatch,
    ReplyGenerator,
    ToolCallFailedError,
    connector_command,
    generated_replies,
    injected_faults,
    prompt_vocabulary,
)
from vs_runtime.api import RuntimeContractError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vs_agent.api import SessionStore, SkillSelection
    from vs_agent.api.testing import FakeInvocation

ROLES = (ORCHESTRATOR.id, IMPLEMENTER.id, JUDGE.id, PROFILER.id)
TOOLS = (
    "trusted_operations",
    "evaluation_availability",
    "submit_evaluation",
    "evaluation_status",
    "await_evaluation",
    "cancel_evaluation",
    "accepted_evidence",
    "profiler_operations",
    "dispatch_profiler",
    "profiler_status",
    "await_profiler",
    "cancel_profiler",
)
#: Outcomes a run may end with: a typed run result or a typed agent failure.
TYPED_ENDS = (
    RunStopped,
    DynamicPlanningError,
    RuntimeContractError,
    AgentCrashError,
    AgentOutputSchemaError,
)
#: A deadlock guard: a chaos run finishes in seconds; raising it never turns a hang into a pass.
RUN_GUARD_S = 600.0
_MAX_TOOL_CALLS = 6
# The tool a correct agent reads its citable evidence from, and the share of
# turns that read it first; the rest act carelessly.
_EVIDENCE_TOOL = "accepted_evidence"
_READS_EVIDENCE = 0.5
_POLL_S = 0.2
# The chance an implementer edits its candidate again after a tool call.
_EDIT_AFTER_CALL = 0.2
_TRANSPORT_FAILURES = frozenset(
    {
        str(EvaluationServiceClientError.incomplete()),
        str(EvaluationServiceClientError.oversized()),
    }
)
_TOOL_WAIT_S = 5.0


class ChaosInvariant:
    """Invariants only a chaos run checks (the shared ones live in ``loop_invariants``)."""

    HANG = "hang"
    UNTYPED_END = "untyped_end"
    STATE_UNLOADABLE = "state_unloadable"
    TOOL_HANDLER_CRASH = "tool_handler_crash"


def repro(seed: int) -> str:
    """Return the one-line command that reruns ``seed``."""
    return (
        "uv run pytest tests/vibesys/orchestration/dynamic/loop/test_chaos.py "
        f"-k 'seed_{seed}]' -p no:randomly  # or CHAOS_SEEDS={seed}"
    )


@dataclass
class ChaosAgents:
    """Generated agents for every role behind the plan's agent-turn and tool-call faults."""

    plan: FaultPlan
    #: Request a stop when the ``stop_at``-th agent turn starts (``None``: never).
    stop_at: int | None = None
    session: Any = None
    tool_errors: list[str] = field(default_factory=list)
    faulty: FaultyAgentClient | None = None
    _counts: Counter[str] = field(default_factory=Counter)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def client(
        self,
        *,
        session_store: SessionStore | None = None,
        skill_selection: SkillSelection = NULL_SKILL_SELECTION,
    ) -> FaultyAgentClient:
        """Build the run's client: a production-capable Fake behind the fault wrapper."""
        fake = FakeAgentClient(
            capabilities=AgentCapabilities(
                tool_servers=True, session_reuse=True, provider_session_resume=True
            ),
            session_reuse=True,
            session_store=session_store,
            skill_selection=skill_selection,
        )
        replies = generated_replies(self.plan)

        def answer(invocation: FakeInvocation) -> BaseModel:
            seen = self._act(invocation)
            return replies(invocation, seen)

        for role in ROLES:
            fake.set_response(role, answer)
        self.faulty = FaultyAgentClient(fake, self.plan)
        return self.faulty

    def _act(self, invocation: FakeInvocation) -> tuple[str, ...]:
        """Edit the candidate (implementers) and call the turn's tools in a generated order.

        Return the identifiers the tool replies named, which the turn's reply may cite.
        """
        with self._lock:
            self._counts[invocation.kind] += 1
            ordinal = self._counts[invocation.kind]
            turns = self._counts.total()
        if turns == self.stop_at and self.session is not None:
            # The user's Ctrl-C reaches a run as this request (the engine's
            # signal handler calls it); the turn itself continues.
            self.session.stop()
        rng = self.plan.rng("act", invocation.kind, ordinal)
        candidate = invocation.workspace / "queue.py"
        editing = invocation.kind == IMPLEMENTER.id and candidate.is_file()
        if editing:
            _edit(candidate, rng.randint(-1, 9), rng.choice((None, 3, 12)))
        tools = _evaluation_tools(invocation)
        dispatch = FaultyToolDispatch(_deliver(tools), self.plan)
        vocabulary = list(prompt_vocabulary(invocation.user_prompt))
        if _EVIDENCE_TOOL in tools and rng.random() < _READS_EVIDENCE:
            # A correct agent reads the trusted evidence it may cite (every
            # kind its role is granted) before it answers.
            vocabulary.extend(self._call(dispatch, invocation.kind, _EVIDENCE_TOOL, {}))
        for _ in range(rng.randint(0, _MAX_TOOL_CALLS) if tools else 0):
            name = rng.choice(sorted(tools))
            schema = tools[name].input_schema.model_json_schema()
            arguments = ReplyGenerator(rng, tuple(vocabulary), bold=True).value(schema, schema)
            if not isinstance(arguments, dict):
                continue
            if "timeout_s" in arguments:
                arguments["timeout_s"] = rng.uniform(0.0, _TOOL_WAIT_S)
            vocabulary.extend(self._call(dispatch, invocation.kind, name, arguments))
            if editing and rng.random() < _EDIT_AFTER_CALL:
                _edit(candidate, rng.randint(-1, 9), None)
        return tuple(vocabulary)

    def _call(
        self, dispatch: FaultyToolDispatch, kind: str, name: str, arguments: Mapping[str, object]
    ) -> list[str]:
        """Make one tool call as an agent CLI does; return the ids its reply names."""
        try:
            return _identifiers(dispatch(name, arguments))
        except (ValidationError, ToolCallFailedError):
            return []
        except EvaluationServiceClientError as error:
            # The host's typed rejection reaches the agent as a tool error;
            # a truncated or oversized reply is a transport failure.
            if str(error) in _TRANSPORT_FAILURES:
                self.tool_errors.append(f"{kind} {name}{arguments}: {error!r}")
            return []
        # lint-waiver: LW-150007 [BLE001]; an MCP server answers a handler
        # > exception with an error result, so the agent continues; the
        # > harness records it as a finding instead of ending the turn.
        except Exception as error:  # noqa: BLE001
            self.tool_errors.append(f"{kind} {name}{arguments}: {error!r}")
            return []


def _evaluation_tools(invocation: FakeInvocation) -> dict[str, Any]:
    """Return the evaluation tools the turn's MCP server offers, by name (none without one)."""
    server = next(
        (item for item in invocation.tool_servers or [] if item.name == "vs-evaluation"), None
    )
    if server is None:
        return {}
    env = {**dict(server.env), **dict(server.runtime_env)}
    return {
        tool.name: tool
        for tool in build_evaluation_tools(
            socket_path=Path(env["VS_EVALUATION_SOCKET"]),
            token=env["VS_EVALUATION_TOKEN"],
            role=EvaluationAgentRole(env["VS_EVALUATION_ROLE"]),
            profiler_available=env.get("VS_EVALUATION_PROFILER_AVAILABLE") == "1",
            run_observer=env.get("VS_EVALUATION_RUN_OBSERVER") == "1",
            evaluation_suspension=env.get("VS_EVALUATION_SUSPENSION") == "1",
        )
    }


def _deliver(tools: Mapping[str, Any]) -> Callable[[str, Mapping[str, object]], dict[str, object]]:
    """Return the dispatcher an MCP server runs: validate the arguments, run the handler."""

    def deliver(name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        tool = tools[name]
        reply = json.loads(tool.handler(tool.input_schema.model_validate(arguments)))
        return reply if isinstance(reply, dict) else {"result": reply}

    return deliver


def _edit(candidate: Path, value: int, required: int | None) -> None:
    text = f"VALUE = {value}\n" + (f"REQUIRED = {required}\n" if required is not None else "")
    candidate.write_text(text, encoding="utf-8")


def _identifiers(reply: object) -> list[str]:
    """Return the id-like strings of a tool reply (handles, operation ids)."""
    if isinstance(reply, dict):
        found = [
            str(value)
            for key, value in reply.items()
            if key.endswith("_id") and isinstance(value, str)
        ]
        return found + [item for value in reply.values() for item in _identifiers(value)]
    if isinstance(reply, list):
        return [item for value in reply for item in _identifiers(value)]
    return []


@dataclass
class ChaosRun:
    """One finished chaos run and every invariant violation it showed."""

    seed: int
    plan: FaultPlan
    run: LoopRun | None
    violations: list[Violation | tuple[str, str]]
    injected: list[str]
    stop_at: int | None = None

    def outcome(self) -> str:
        """Return how the run ended, in one line."""
        if self.run is None:
            return "no end"
        return f"succeeded={self.run.succeeded} error={self.run.error!r}"[:300]

    def summary(self, loop_input: LoopInput) -> dict[str, object]:
        """Return a JSON-ready summary: outcome, workstream phases, faults, violations."""
        phases: Counter[str] = Counter()
        profiles = 0
        if self.run is not None and state_path(loop_input, self.run.run_id).is_file():
            state = json.loads(state_path(loop_input, self.run.run_id).read_text("utf-8"))
            phases.update(str(item["phase"]) for item in state["workstreams"])
            profiles = len(state["profiles"])
        return {
            "seed": self.seed,
            "outcome": self.outcome(),
            "phases": dict(phases),
            "profiles": profiles,
            "injected": self.injected,
            "stop_at": self.stop_at,
            "violations": [str(item) for item in self.violations],
        }

    def report(self) -> str:
        """Return the failure report: seed, repro, injected faults, violations."""
        lines = [
            f"seed {self.seed}: {len(self.violations)} violation(s); {self.outcome()}",
            repro(self.seed),
        ]
        lines += [f"  injected: {item}" for item in self.injected]
        if self.stop_at is not None:
            lines.append(f"  stop requested at agent turn {self.stop_at}")
        lines += [f"  VIOLATION {item}" for item in self.violations]
        return "\n".join(lines)


def plan_for(seed: int, *, faults: int | None = None) -> FaultPlan:
    """Return the fault plan seed ``seed`` runs under (0 to 4 faults)."""
    count = faults if faults is not None else seed % 5
    return FaultPlan.generate(
        seed,
        targets={Boundary.AGENT_TURN: ROLES, Boundary.TOOL_CALL: TOOLS, Boundary.CLUSTER: ()},
        faults=count,
    )


def run_chaos(base: Path, seed: int, plan: FaultPlan | None = None) -> ChaosRun:
    """Run the dynamic loop once under ``seed`` and check every invariant."""
    plan = plan if plan is not None else plan_for(seed)
    faults_dir = base / "faults"
    faults_dir.mkdir(parents=True)
    plan_file = plan.save(faults_dir / "plan.json")
    # Odd seeds run an LLM-serving input on ROCm, so the run provisions the
    # profiler and offers profiles; even seeds offer none.
    loop_input = LoopInput.create(
        base,
        profiled=seed % 2 == 1,
        # A faulted poll is retried; a long interval would stall the run.
        poll_interval_s=_POLL_S,
        connector=lambda inner: connector_command(plan_file, faults_dir / "cluster", inner),
    )
    rng = plan.rng("options")
    agents = ChaosAgents(plan, stop_at=rng.choice((None, None, None, rng.randint(1, 8))))
    configured = options(
        max_rounds=rng.randint(1, 3),
        max_in_flight=rng.randint(1, 3),
        max_retries_per_round=rng.randint(1, 2),
    )
    finished: list[LoopRun] = []
    escaped: list[BaseException] = []

    def drive() -> None:
        try:
            finished.append(
                run_loop(
                    loop_input,
                    agents,
                    configured,
                    on_session=lambda session: setattr(agents, "session", session),
                )
            )
        # lint-waiver: LW-150012 [BLE001]; whatever escapes the session
        # > (the harness catches Exception) is the finding, BaseException included.
        except BaseException as error:  # noqa: BLE001
            escaped.append(error)

    thread = threading.Thread(target=drive, daemon=True)
    thread.start()
    thread.join(RUN_GUARD_S)
    injected = [str(item) for item in (agents.faulty.injected if agents.faulty else [])]
    injected += [json.dumps(item) for item in injected_faults(faults_dir / "cluster")]
    violations: list[Violation | tuple[str, str]] = [
        (ChaosInvariant.TOOL_HANDLER_CRASH, error) for error in agents.tool_errors
    ]
    if escaped:
        violations.append((ChaosInvariant.UNTYPED_END, f"escaped the session: {escaped[0]!r}"))
    elif not finished:
        violations.append((ChaosInvariant.HANG, f"run did not end within {RUN_GUARD_S} s"))
    if not finished:
        return ChaosRun(seed, plan, None, violations, injected, agents.stop_at)
    run = finished[0]
    if run.error is not None and not isinstance(run.error, TYPED_ENDS):
        violations.append((ChaosInvariant.UNTYPED_END, repr(run.error)))
    # A profile that failed because the plan faulted its agent or its job, or
    # because the run stopped, is a typed failure, not an unservable capability.
    profiles_faulted = agents.stop_at is not None or any(
        PROFILER.id in item or "operation" in item for item in injected
    )
    violations += _records_violations(loop_input, run, profiles_faulted=profiles_faulted)
    chaos = ChaosRun(seed, plan, run, violations, injected, agents.stop_at)
    log = os.environ.get("CHAOS_LOG")
    if log:
        with Path(log).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(chaos.summary(loop_input)) + "\n")
    return chaos


def _records_violations(
    loop_input: LoopInput, run: LoopRun, *, profiles_faulted: bool
) -> list[Violation | tuple[str, str]]:
    path = state_path(loop_input, run.run_id)
    state: dict[str, object] | None = None
    found: list[Violation | tuple[str, str]] = []
    if path.is_file():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            load_state(loop_input, run.run_id)
        # lint-waiver: LW-150009 [BLE001]; any failure to load the state the
        # > run left is the finding; naming types would hide a new failure mode.
        except Exception as error:  # noqa: BLE001
            found.append((ChaosInvariant.STATE_UNLOADABLE, repr(error)))
    jobs_dir = loop_input.cluster / "jobs"
    jobs = (
        {item.name: item.read_text(encoding="utf-8").strip() for item in jobs_dir.iterdir()}
        if jobs_dir.is_dir()
        else {}
    )
    records = RunRecords(
        events=[event.model_dump(mode="json") for event in run.events],
        state=state,
        cluster_jobs=jobs,
    )
    found += [
        violation
        for violation in check(records, roots=(loop_input.root,))
        # The Fake agent client records no token usage.
        if violation.invariant is not Invariant.USAGE_UNRECORDED
        and not (
            profiles_faulted
            and violation.invariant is Invariant.CAPABILITY_UNSERVED
            and "never served or withdrawn" in violation.detail
        )
    ]
    return found
