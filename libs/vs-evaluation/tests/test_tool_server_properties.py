"""Property tests for the evaluation agent MCP tools, driven without agents.

Every tool that ``build_evaluation_tools`` returns is called with arguments
generated from its own input schema against a real evaluation service on a
Unix socket, backed by the package's Fakes. A new tool is covered without new
test code. Each reply must be a JSON object within the client's size limit, or
a typed refusal; a role's unauthorized calls are refused; and no caller changes
an evaluation or profiler turn it does not own.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    precondition,
    rule,
)
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ValidationError

from vs_agent.api import register_tool
from vs_async_ops.api.testing import ImmediateTimeoutWaiter
from vs_evaluation.api import (
    MAX_PROFILER_REQUEST_CHARS,
    AvailabilityCall,
    AvailabilitySnapshot,
    AwaitCall,
    AwaitProfilerCall,
    CancelCall,
    CanceledReply,
    CancelProfilerCall,
    ContentDigest,
    DispatchProfilerCall,
    EvaluationAgentRole,
    EvaluationAgentService,
    EvaluationAwaitResult,
    EvaluationCoordinator,
    EvaluationGrant,
    EvaluationOperationSnapshot,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvidenceCall,
    EvidenceFingerprints,
    EvidenceKind,
    ProfilerAgentService,
    ProfilerAgentServiceHooks,
    ProfilerOperationsCall,
    ProfilerStatusCall,
    ProfilerWorkKey,
    ProfilerWorkPurpose,
    ResourceRequirements,
    RunOperationsCall,
    ScopeSubmissionTracker,
    StatusCall,
    StoredEvaluation,
    SubmitCall,
    SubmittedReply,
    SubmittedSemanticEvaluation,
    TrustedEvidence,
    stable_handle_id,
)
from vs_evaluation.api.testing import (
    FakeClock,
    FakeEvaluationExecutor,
    FakeProfilerTurnProvision,
    InMemoryEvaluationStore,
)
from vs_evaluation.api.tools import (
    EvaluationServiceClientError,
    build_evaluation_tools,
    evaluation_tool_names,
)
from vs_project.api import (
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
    RunExecutionRecord,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Iterator

    from vs_agent.api import ToolSpec

# The socket client's documented reply bound: a larger reply raises
# EvaluationServiceClientError.oversized() (vs_evaluation/agent_mcp.py).
_MAX_REPLY_BYTES = 1_048_576
# The service's request frame bound (vs_evaluation/agent_service.py).
_MAX_FRAME_BYTES = 1_048_576
# Deadlock guard for one call into the service loop; raising it cannot turn a
# pass into a failure, because every Fake wait returns without wall-clock time.
_LOOP_GUARD_S = 60.0
_ROLES = tuple(EvaluationAgentRole)
_VICTIM = "victim"
_VICTIM_SCOPE = "victim-scope"
_WORK = ProfilerWorkKey(purpose=ProfilerWorkPurpose.TARGETED_DIAGNOSTIC, focus="victim focus")
_TERMINAL = frozenset({EvaluationState.SUCCEEDED, EvaluationState.FAILED, EvaluationState.CANCELED})


def _fingerprints(seed: str) -> EvidenceFingerprints:
    return EvidenceFingerprints(
        candidate=ContentDigest.sha256(seed.encode()),
        evaluator=ContentDigest.sha256(b"evaluator"),
        workload=ContentDigest.sha256(b"workload"),
        environment=ContentDigest.sha256(b"environment"),
    )


class _CoordinatorBackend:
    """Faithful semantic backend Fake over the provider-neutral coordinator."""

    def __init__(self, coordinator: EvaluationCoordinator) -> None:
        self._coordinator = coordinator
        self._submissions = ScopeSubmissionTracker()

    async def availability(self, requirements: ResourceRequirements) -> AvailabilitySnapshot:
        return await self._coordinator.availability(requirements)

    async def submit_evidence(
        self,
        scope_id: str | None,
        kinds: tuple[EvidenceKind, ...],
        *,
        own: Callable[[SubmittedSemanticEvaluation], Awaitable[None]],
    ) -> SubmittedSemanticEvaluation:
        async with self._submissions.track(scope_id):
            fingerprints = _fingerprints(scope_id or "root")
            key = f"{fingerprints.candidate.value}:{','.join(kind.value for kind in kinds)}"
            request = EvaluationRequest(
                key=key,
                owner_scope=scope_id,
                stages=tuple(
                    EvaluationStep(
                        name=kind.value,
                        payload={
                            "semantic": kind.value,
                            "fingerprints": fingerprints.model_dump(mode="json"),
                        },
                    )
                    for kind in kinds
                ),
            )
            await self._coordinator.prepare(request)
            await own(
                SubmittedSemanticEvaluation(
                    handle_id=stable_handle_id(key), fingerprints=fingerprints
                )
            )
            self._submissions.check_admission()
            handle = await self._coordinator.submit(request)
            return SubmittedSemanticEvaluation(handle_id=handle.id, fingerprints=fingerprints)

    def restarted(self) -> _CoordinatorBackend:
        """Create a fresh process handler over the same durable request authority."""
        return _CoordinatorBackend(self._coordinator)

    async def drain_submissions(self, scope_id: str | None) -> None:
        """Join any submission admitted before closure."""
        await self._submissions.drain(scope_id)

    async def accepted_evidence(
        self, scope_id: str | None, kinds: tuple[EvidenceKind, ...]
    ) -> tuple[TrustedEvidence, ...]:
        del scope_id, kinds
        return ()

    async def owned_handles(self, scope_id: str | None) -> tuple[str, ...]:
        """Read the scope identity durably attached to each claimed request."""
        return tuple(
            record.handle_id
            for record in await self._coordinator.history()
            if scope_id is None or record.request.owner_scope == scope_id
        )

    async def recorded_snapshot(self, handle_id: str) -> StoredEvaluation:
        return await self._coordinator.recorded_snapshot(handle_id)

    async def recorded_submission(self, handle_id: str) -> SubmittedSemanticEvaluation | None:
        record = await self._coordinator.recorded_snapshot(handle_id)
        payload = record.request.stages[0].payload
        if not isinstance(payload, dict) or "fingerprints" not in payload:
            return None
        return SubmittedSemanticEvaluation(
            handle_id=handle_id,
            fingerprints=EvidenceFingerprints.model_validate(payload["fingerprints"]),
        )

    async def recorded_status(self, handle_id: str) -> EvaluationState:
        """Read committed state without dispatching work."""
        return await self._coordinator.recorded_status(handle_id)

    async def status(self, handle_id: str) -> EvaluationState:
        return await self._coordinator.status(handle_id)

    async def operation_snapshot(self, handle_id: str) -> EvaluationOperationSnapshot:
        record = await self._coordinator.snapshot(handle_id)
        return EvaluationOperationSnapshot(
            handle_id=handle_id,
            state=record.state,
            current_stage=record.current_stage,
            evidence_recorded=False,
        )

    async def await_result(self, handle_id: str, timeout_s: float) -> EvaluationAwaitResult:
        return await self._coordinator.await_result(handle_id, timeout_s)

    async def cancel(self, handle_id: str) -> StoredEvaluation:
        return await self._coordinator.cancel(handle_id)


async def _candidate_snapshot(scope_id: str | None) -> str:
    return f"snapshot:{scope_id}"


async def _no_evidence(
    principal_id: str, scope_id: str | None, snapshot: str, evidence_ids: tuple[str, ...]
) -> tuple[TrustedEvidence, ...]:
    del principal_id, scope_id, snapshot, evidence_ids
    return ()


@dataclass(frozen=True, slots=True)
class _Actor:
    """One grant holder; ``grant`` is None for a forged token."""

    token: str
    grant: EvaluationGrant | None

    @property
    def principal(self) -> str | None:
        return None if self.grant is None else self.grant.principal_id

    def granted_tools(self) -> frozenset[str]:
        """The public tool surface for this grant: the refusal oracle."""
        if self.grant is None:
            return frozenset()
        return frozenset(
            evaluation_tool_names(
                self.grant.role,
                profiler_available=self.grant.profiler_available,
                run_observer=self.grant.run_observer,
            )
        )


@dataclass(slots=True)
class _Outcome:
    """A tool call's observable result: a JSON object or a typed refusal."""

    document: dict[str, Any] | None
    refusal: str | None


@dataclass(slots=True)
class _World:
    """A started evaluation service on its own event-loop thread."""

    root: Path
    loop: asyncio.AbstractEventLoop = field(default_factory=asyncio.new_event_loop)
    thread: threading.Thread | None = None
    service: EvaluationAgentService | None = None
    profiler: ProfilerAgentService | None = None
    executor: FakeEvaluationExecutor | None = None
    backend: _CoordinatorBackend | None = None
    provision: FakeProfilerTurnProvision | None = None
    evaluation_owners: dict[str, set[str]] = field(default_factory=dict)
    victim_handle: str = ""
    victim_operation: str = ""
    actors: list[_Actor] = field(default_factory=list)

    def run[T](self, coroutine: Coroutine[Any, Any, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(_LOOP_GUARD_S)

    def start(self) -> None:
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.run(self._build())

    async def _build(self) -> None:
        project = Project.open(self.root)
        project.state.create_project("test")
        manifest = project.state.new_run_manifest(
            "Tool server properties",
            run_id="tool-server-properties",
            trusted_input_baseline="a" * 40,
            branch="test/tool-server-properties",
            vibesys_version="test",
            run_environment=RunEnvironmentRecord(name="local"),
            execution=RunExecutionRecord(
                model="test-model",
                agent_backend="stub",
                compute_backend="cpu",
                requested_profiler="none",
                resolved_profiler="none",
                agent_roles={},
            ),
            orchestration=OrchestrationDescriptor(id="test", config_version=1, options={}),
        )
        project.state.create_run(manifest)
        clock = FakeClock()
        self.executor = FakeEvaluationExecutor(
            clock, supported_evidence_kinds=tuple(kind.value for kind in EvidenceKind)
        )
        self.backend = _CoordinatorBackend(
            EvaluationCoordinator(
                self.executor, InMemoryEvaluationStore(), clock, max_await_timeout_s=45
            )
        )
        self.provision = FakeProfilerTurnProvision()
        self.profiler = ProfilerAgentService(
            self.provision,
            project.state.local_namespace(manifest.run_id, "profiler-agent"),
            ProfilerAgentServiceHooks(
                candidate_snapshot=_candidate_snapshot,
                resolve_evidence=_no_evidence,
                waiter=ImmediateTimeoutWaiter(),
            ),
        )
        self.service = EvaluationAgentService(
            self.backend,
            project.state.local_namespace(manifest.run_id, "evaluation-agent"),
            self.root / "e.sock",
            profiler_agents=self.profiler,
        )
        victim = self.service.grant(
            principal_id=_VICTIM, role=EvaluationAgentRole.IMPLEMENTER, scope_id=_VICTIM_SCOPE
        )
        submitted = await self.service.dispatch(
            SubmitCall(token=victim.token, evidence_kinds=(EvidenceKind.ACCURACY,))
        )
        assert isinstance(submitted, SubmittedReply)
        self.victim_handle = submitted.handle_id
        self.evaluation_owners[submitted.handle_id] = {_VICTIM}
        dispatched = await self.service.dispatch(
            DispatchProfilerCall(token=victim.token, work=_WORK, request="profile the victim")
        )
        self.victim_operation = json.loads(dispatched.model_dump_json())["operation_id"]
        self._grant_actors()
        await self.service.start()

    def _grant_actors(self) -> None:
        assert self.service is not None
        self.actors = [
            _Actor(grant.token, grant)
            for role in _ROLES
            for scope in (f"scope-{role.value}", _VICTIM_SCOPE)
            for observer in (False, True)
            for grant in (
                self.service.grant(
                    principal_id=f"{role.value}-{scope}",
                    role=role,
                    scope_id=scope,
                    run_observer=observer,
                ),
            )
        ]
        self.actors.append(_Actor(secrets.token_urlsafe(32), None))

    def stop(self) -> None:
        assert self.service is not None
        self.run(self.service.close())

    def restart(self) -> None:
        assert self.backend is not None
        self.backend = self.backend.restarted()
        self.service = EvaluationAgentService(
            self.backend,
            Project.open(self.root).state.local_namespace(
                "tool-server-properties", "evaluation-agent"
            ),
            self.root / "e.sock",
            profiler_agents=self.profiler,
        )
        self.run(self.service.start())
        self._grant_actors()

    def victim_state(self) -> tuple[EvaluationState, bool]:
        """The victim's evaluation state and whether its profiler turn was canceled."""
        assert self.backend is not None
        assert self.provision is not None
        state = self.run(self.backend.status(self.victim_handle))
        return state, self.victim_operation in self.provision.canceled

    def set_state(self, handle_id: str, state: EvaluationState) -> None:
        executor = self.executor
        assert executor is not None

        async def publish() -> None:
            failure = "remote failure" if state is EvaluationState.FAILED else None
            executor.set_state(handle_id, state, failure=failure)

        self.run(publish())

    def close(self) -> None:
        if self.service is not None:
            self.run(self.service.close())
        if self.profiler is not None:
            self.run(self.profiler.close())
        self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread is not None:
            self.thread.join(_LOOP_GUARD_S)
        self.loop.close()

    def tools(self, actor: _Actor, role: EvaluationAgentRole) -> tuple[ToolSpec[Any], ...]:
        """The widest tool surface for ``role``, bound to ``actor``'s token."""
        assert self.service is not None
        return build_evaluation_tools(
            socket_path=self.service.socket_path,
            token=actor.token,
            role=role,
            profiler_available=True,
            run_observer=True,
        )


@contextmanager
def _world() -> Iterator[_World]:
    with tempfile.TemporaryDirectory(prefix="vs-tsp-") as root:
        world = _World(Path(root))
        world.start()
        try:
            yield world
        finally:
            world.close()


def _call(tool: ToolSpec[Any], args: BaseModel) -> _Outcome:
    """Call one handler; any exception other than the typed refusal propagates."""
    try:
        raw = tool.handler(args)
    except EvaluationServiceClientError as error:
        return _Outcome(document=None, refusal=str(error))
    assert isinstance(raw, str)
    assert len(raw.encode()) <= _MAX_REPLY_BYTES
    document = json.loads(raw)
    assert isinstance(document, dict)
    return _Outcome(document=document, refusal=None)


def _ids(value: object) -> Iterator[str]:
    """Every handle, operation, and session id in a reply document."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"handle_id", "operation_id", "session_id"} and isinstance(item, str):
                yield item
            yield from _ids(item)
    elif isinstance(value, list):
        for item in value:
            yield from _ids(item)


_EXTREME_SIZES = (0, 1, 512, 513, MAX_PROFILER_REQUEST_CHARS + 1, _MAX_FRAME_BYTES + 1)


def _strings(pool: list[str]) -> st.SearchStrategy[str]:
    return st.one_of(
        st.sampled_from(pool),
        st.text(max_size=24),
        st.builds(lambda char, size: char * size, st.characters(), st.sampled_from(_EXTREME_SIZES)),
    )


def _junk() -> st.SearchStrategy[object]:
    return st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=8), st.just([]))


def _from_schema(
    schema: dict[str, Any], defs: dict[str, Any], pool: list[str]
) -> st.SearchStrategy[object]:
    """Values for one JSON-schema node, mostly well-typed, sometimes not."""
    if "$ref" in schema:
        return _from_schema(defs[schema["$ref"].rsplit("/", 1)[-1]], defs, pool)
    if "anyOf" in schema:
        return st.one_of(*(_from_schema(option, defs, pool) for option in schema["anyOf"]))
    if "enum" in schema:
        return st.one_of(st.sampled_from(schema["enum"]), _strings(pool))
    kind = schema.get("type")
    if kind == "object":
        return _object(schema, defs, pool)
    if kind == "array":
        item = _from_schema(schema.get("items", {}), defs, pool)
        typed = st.one_of(st.lists(item, max_size=4), item.map(lambda value: [value, value]))
    else:
        scalars: dict[str, st.SearchStrategy[object]] = {
            "string": _strings(pool),
            "number": st.one_of(st.sampled_from([1e-9, 0.001, 1.0, 45.0, 46.0, 1e9]), st.floats()),
            "integer": st.integers(),
            "boolean": st.booleans(),
            "null": st.none(),
        }
        typed = scalars.get(kind, _junk())
    return st.one_of(typed, _junk())


def _object(
    schema: dict[str, Any], defs: dict[str, Any], pool: list[str]
) -> st.SearchStrategy[object]:
    properties: dict[str, Any] = schema.get("properties", {})
    required = set(schema.get("required", ()))
    fields = st.fixed_dictionaries(
        {
            name: _from_schema(node, defs, pool)
            for name, node in properties.items()
            if name in required
        },
        optional={
            name: _from_schema(node, defs, pool)
            for name, node in properties.items()
            if name not in required
        },
    )
    unknown = st.dictionaries(st.sampled_from(["unknown", "token", "role"]), _junk(), max_size=1)
    return st.builds(lambda known, extra: {**extra, **known}, fields, unknown)


def _arguments(tool: ToolSpec[Any], pool: list[str]) -> st.SearchStrategy[object]:
    schema = tool.input_schema.model_json_schema()
    return _from_schema(schema, schema.get("$defs", {}), pool)


def _check_call(world: _World, actor: _Actor, tool: ToolSpec[Any], args: BaseModel) -> _Outcome:
    """Call one tool and assert the per-reply and ownership properties."""
    before = world.victim_state()
    outcome = _call(tool, args)
    if tool.name not in actor.granted_tools():
        assert outcome.refusal is not None, (
            f"{tool.name} is outside the {actor.grant and actor.grant.role} surface "
            f"but returned {outcome.document}"
        )
    if (
        tool.name == "submit_evaluation"
        and outcome.document is not None
        and actor.principal is not None
    ):
        handle_id = outcome.document.get("handle_id")
        if isinstance(handle_id, str):
            world.evaluation_owners.setdefault(handle_id, set()).add(actor.principal)
    may_cancel_victim = actor.grant is not None and (
        actor.principal in world.evaluation_owners.get(world.victim_handle, set())
        or actor.grant.role is EvaluationAgentRole.ORCHESTRATOR
    )
    if not may_cancel_victim:
        assert world.victim_state() == before, f"{tool.name} changed the victim's state"
    return outcome


_SETTINGS = settings(
    max_examples=120,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


def test_every_tool_reply_is_typed_bounded_and_authorized() -> None:
    with _world() as world:
        pool = ["", "unknown", world.victim_handle, world.victim_operation]

        @_SETTINGS
        @given(data=st.data())
        def check(data: st.DataObject) -> None:
            actor = data.draw(st.sampled_from(world.actors), label="actor")
            surface = data.draw(st.sampled_from(_ROLES), label="tool surface")
            tool = data.draw(st.sampled_from(world.tools(actor, surface)), label="tool")
            raw = data.draw(_arguments(tool, pool), label="arguments")
            try:
                args = tool.input_schema.model_validate(raw)
            except ValidationError:
                event("input rejected by the tool schema")
                return
            outcome = _check_call(world, actor, tool, args)
            event(f"{tool.name}: {'refused' if outcome.refusal is not None else 'reply'}")
            pool.extend(sorted(set(_ids(outcome.document)) - set(pool)))

        check()


@dataclass(slots=True)
class _Tracked:
    """A handle or operation id with the actor that minted it."""

    value: str
    owner: _Actor


class _ToolServerMachine(RuleBasedStateMachine):
    """Call sequences across tools, service stop, and restart."""

    handles = Bundle("handles")
    operations = Bundle("operations")
    allow_stop = False

    def __init__(self) -> None:
        super().__init__()
        self._scope = _world()
        self.world = self._scope.__enter__()
        self.running = True
        self.terminal: dict[str, str] = {}

    def teardown(self) -> None:
        self._scope.__exit__(None, None, None)

    def _actor(self, role: EvaluationAgentRole, *, shared_scope: bool) -> _Actor:
        scope = _VICTIM_SCOPE if shared_scope else f"scope-{role.value}"
        return next(
            actor
            for actor in self.world.actors
            if actor.grant is not None
            and actor.grant.role is role
            and actor.grant.scope_id == scope
            and not actor.grant.run_observer
        )

    def _invoke(self, actor: _Actor, name: str, raw: dict[str, object]) -> _Outcome:
        tools = {
            tool.name: tool for tool in self.world.tools(actor, EvaluationAgentRole.IMPLEMENTER)
        }
        tool = tools[name]
        try:
            args = tool.input_schema.model_validate(raw)
        except ValidationError as error:
            # The MCP layer validates against the same schema before the handler.
            return _Outcome(document=None, refusal=str(error))
        if not self.running:
            before = self.world.victim_state()
            outcome = _call(tool, args)
            assert outcome.refusal is not None, f"{name} succeeded on a stopped service"
            assert self.world.victim_state() == before
            return outcome
        return _check_call(self.world, actor, tool, args)

    @rule(
        target=handles,
        role=st.sampled_from(_ROLES),
        shared=st.booleans(),
        kinds=st.lists(st.sampled_from(EvidenceKind), max_size=3),
    )
    def submit(
        self, *, role: EvaluationAgentRole, shared: bool, kinds: list[EvidenceKind]
    ) -> object:
        actor = self._actor(role, shared_scope=shared)
        outcome = self._invoke(actor, "submit_evaluation", {"evidence_kinds": kinds})
        if outcome.document is None:
            return _Tracked(f"never-minted-{role.value}", actor)
        return _Tracked(outcome.document["handle_id"], actor)

    @rule(role=st.sampled_from(_ROLES), handle=st.one_of(handles, st.just(None)))
    def status(self, role: EvaluationAgentRole, handle: _Tracked | None) -> None:
        actor = self._actor(role, shared_scope=False)
        handle_id = handle.value if handle is not None else "await-before-submit"
        for _ in range(2):
            outcome = self._invoke(actor, "evaluation_status", {"handle_id": handle_id})
            self._observe(handle_id, outcome)

    @rule(
        handle=st.one_of(handles, st.just(None)),
        timeout_s=st.sampled_from([1e-6, 0.01, 1.0, 45.0, 1e6]),
    )
    def await_(self, handle: _Tracked | None, timeout_s: float) -> None:
        actor = handle.owner if handle is not None else self.world.actors[0]
        handle_id = handle.value if handle is not None else "await-before-submit"
        outcome = self._invoke(
            actor, "await_evaluation", {"handle_id": handle_id, "timeout_s": timeout_s}
        )
        if handle is None:
            assert outcome.refusal is not None

    @rule(handle=handles, by_owner=st.booleans(), role=st.sampled_from(_ROLES))
    def cancel_twice(self, *, handle: _Tracked, by_owner: bool, role: EvaluationAgentRole) -> None:
        actor = handle.owner if by_owner else self._actor(role, shared_scope=False)
        for _ in range(2):
            outcome = self._invoke(actor, "cancel_evaluation", {"handle_id": handle.value})
            self._observe(handle.value, outcome)

    @rule(
        handle=consumes(handles),
        state=st.sampled_from([EvaluationState.FAILED, EvaluationState.CANCELED]),
    )
    def finish(self, handle: _Tracked, state: EvaluationState) -> None:
        if handle.value.startswith("never-minted-"):
            return
        self.world.set_state(handle.value, state)

    @rule(target=operations, request=st.sampled_from(["profile it", "x" * 1024]))
    def dispatch_profiler(self, request: str) -> object:
        actor = self._actor(EvaluationAgentRole.IMPLEMENTER, shared_scope=False)
        outcome = self._invoke(
            actor,
            "dispatch_profiler",
            {"work": _WORK.model_dump(mode="json"), "request": request},
        )
        if outcome.document is None:
            return _Tracked("never-minted-operation", actor)
        return _Tracked(outcome.document["operation_id"], actor)

    @rule(
        operation=st.one_of(operations, st.just(None)),
        shared=st.booleans(),
        action=st.sampled_from(["profiler_status", "await_profiler", "cancel_profiler"]),
    )
    def profiler_twice(self, *, operation: _Tracked | None, shared: bool, action: str) -> None:
        actor = (
            operation.owner
            if operation is not None and not shared
            else self._actor(EvaluationAgentRole.IMPLEMENTER, shared_scope=shared)
        )
        operation_id = operation.value if operation is not None else self.world.victim_operation
        raw: dict[str, object] = {"operation_id": operation_id}
        if action == "await_profiler":
            raw["timeout_s"] = 0.01
        for _ in range(2):
            self._invoke(actor, action, raw)

    @precondition(lambda self: self.allow_stop and self.running)
    @rule()
    def stop(self) -> None:
        self.world.stop()
        self.running = False

    @precondition(lambda self: self.allow_stop and not self.running)
    @rule()
    def restart(self) -> None:
        stale = list(self.world.actors)
        self.world.restart()
        self.running = True
        outcome = self._invoke(stale[0], "evaluation_availability", {})
        assert outcome.refusal is not None, "a grant survived service close"

    def _observe(self, handle_id: str, outcome: _Outcome) -> None:
        """A terminal state, once reported for a handle, never changes."""
        if outcome.document is None:
            return
        state = outcome.document.get("status")
        if not isinstance(state, str):
            return
        if handle_id in self.terminal:
            assert state == self.terminal[handle_id], f"{handle_id} left terminal state"
        elif EvaluationState(state) in _TERMINAL:
            self.terminal[handle_id] = state


_MACHINE_SETTINGS = settings(
    max_examples=8,
    stateful_step_count=10,
    suppress_health_check=[HealthCheck.too_slow],
)


class _RunningServiceMachine(_ToolServerMachine):
    allow_stop = False


class _StoppableServiceMachine(_ToolServerMachine):
    allow_stop = True


TestCallSequencesOnARunningService = _RunningServiceMachine.TestCase
TestCallSequencesOnARunningService.settings = _MACHINE_SETTINGS
TestCallSequencesAcrossServiceStop = _StoppableServiceMachine.TestCase
TestCallSequencesAcrossServiceStop.settings = _MACHINE_SETTINGS


def _implementer_tools(world: _World) -> dict[str, ToolSpec[Any]]:
    actor = next(
        actor
        for actor in world.actors
        if actor.grant is not None and actor.grant.role is EvaluationAgentRole.IMPLEMENTER
    )
    return {tool.name: tool for tool in world.tools(actor, EvaluationAgentRole.IMPLEMENTER)}


# Each tool's wire call model: the service validates this, so the schema the
# agent is offered must equal its agent-supplied part.
_WIRE_CALLS: dict[str, type[BaseModel]] = {
    "trusted_operations": RunOperationsCall,
    "evaluation_availability": AvailabilityCall,
    "submit_evaluation": SubmitCall,
    "evaluation_status": StatusCall,
    "await_evaluation": AwaitCall,
    "cancel_evaluation": CancelCall,
    "profiler_operations": ProfilerOperationsCall,
    "dispatch_profiler": DispatchProfilerCall,
    "profiler_status": ProfilerStatusCall,
    "await_profiler": AwaitProfilerCall,
    "cancel_profiler": CancelProfilerCall,
    "accepted_evidence": EvidenceCall,
}
_HOST_FIELDS = frozenset({"action", "token"})


def _offered(tools: tuple[ToolSpec[Any], ...]) -> dict[str, dict[str, Any]]:
    """The input schema of each tool as the MCP server lists it to an agent."""
    server = FastMCP("offered")
    for tool in tools:
        register_tool(server, tool)
    return {listed.name: listed.inputSchema for listed in asyncio.run(server.list_tools())}


@pytest.mark.parametrize("role", _ROLES, ids=lambda role: role.value)
def test_every_offered_tool_schema_is_its_wire_models_agent_fields(
    role: EvaluationAgentRole,
) -> None:
    tools = build_evaluation_tools(
        socket_path=Path("/unused"),
        token=secrets.token_urlsafe(8),
        role=role,
        profiler_available=True,
        run_observer=True,
    )
    offered = _offered(tools)

    assert set(offered) <= set(_WIRE_CALLS)
    for name, schema in offered.items():
        wire = _WIRE_CALLS[name].model_json_schema()
        agent_fields = {
            key: value for key, value in wire["properties"].items() if key not in _HOST_FIELDS
        }
        assert schema["properties"] == agent_fields, name
        assert set(schema.get("required", ())) == set(wire.get("required", ())) - _HOST_FIELDS
        assert schema.get("$defs") == wire.get("$defs"), name


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("evaluation_status", {"handle_id": ""}),
        ("cancel_evaluation", {"handle_id": ""}),
        ("profiler_status", {"operation_id": ""}),
        ("submit_evaluation", {"evidence_kinds": ["accuracy", "accuracy"]}),
        ("accepted_evidence", {"evidence_kinds": ["profile", "profile"]}),
        ("dispatch_profiler", {"work": _WORK.model_dump(mode="json"), "request": " padded "}),
    ],
)
def test_an_input_the_wire_model_rejects_is_rejected_by_the_offered_schema(
    name: str, raw: dict[str, object]
) -> None:
    """These inputs once passed the tool schema and raised a raw ValidationError."""
    with _world() as world:
        tool = _implementer_tools(world)[name]
        with pytest.raises(ValidationError):
            tool.input_schema.model_validate(raw)
        server = FastMCP("offered")
        register_tool(server, tool)
        with pytest.raises(ToolError, match="validation error"):
            asyncio.run(server.call_tool(name, raw))


@given(timeout_s=st.floats(min_value=1e-9, max_value=1e9))
@settings(max_examples=25, deadline=None)
def test_an_await_longer_than_the_cap_is_served_capped(timeout_s: float) -> None:
    """The offered await accepts any positive wait, and the service caps it."""
    with _world() as world:
        tools = _implementer_tools(world)
        submit = tools["submit_evaluation"]
        submitted = _call(submit, submit.input_schema.model_validate({}))
        assert submitted.document is not None, submitted.refusal
        wait = tools["await_evaluation"]
        outcome = _call(
            wait,
            wait.input_schema.model_validate(
                {"handle_id": submitted.document["handle_id"], "timeout_s": timeout_s}
            ),
        )
        assert outcome.document is not None, outcome.refusal


def test_a_request_larger_than_the_frame_limit_is_a_typed_refusal() -> None:
    with _world() as world:
        tool = _implementer_tools(world)["evaluation_status"]
        outcome = _call(tool, tool.input_schema.model_validate({"handle_id": "x" * (2 << 20)}))
        assert outcome.refusal is not None


def test_same_scope_submitter_joins_ownership_without_revoking_original_owner() -> None:
    """A successful semantic join grants cancellation; sharing a scope alone does not."""
    with _world() as world:
        actor = next(
            actor
            for actor in world.actors
            if actor.grant is not None
            and actor.grant.role is EvaluationAgentRole.IMPLEMENTER
            and actor.grant.scope_id == _VICTIM_SCOPE
        )
        tools = {tool.name: tool for tool in world.tools(actor, EvaluationAgentRole.IMPLEMENTER)}
        cancel = tools["cancel_evaluation"]
        args = cancel.input_schema.model_validate({"handle_id": world.victim_handle})
        denied = _check_call(world, actor, cancel, args)
        assert denied.refusal is not None
        submit = tools["submit_evaluation"]
        submitted = _check_call(
            world,
            actor,
            submit,
            submit.input_schema.model_validate({"evidence_kinds": ["accuracy"]}),
        )
        assert submitted.document is not None
        assert submitted.document["handle_id"] == world.victim_handle
        assert _check_call(world, actor, cancel, args).refusal is None
        assert world.service is not None
        original = world.service.grant(
            principal_id=_VICTIM, role=EvaluationAgentRole.IMPLEMENTER, scope_id=_VICTIM_SCOPE
        )
        reply = world.run(
            world.service.dispatch(CancelCall(token=original.token, handle_id=world.victim_handle))
        )
        assert isinstance(reply, CanceledReply)
        assert reply.status is EvaluationState.CANCELED
