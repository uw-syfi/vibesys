"""Drive a flow search by hand, as a script: no asyncio, no clock, no core.

``FlowRun`` plays the host. It starts the search, keeps the list of waits the search has
opened, answers the prompt renders by itself (they are core's own business), answers at once
the owned operations a story hands to ``own``, and delivers the events a story names::

    run = FlowRun.start(search, config, facts)
    asked = run.expect_turn("worker")          # the next turn request that matches
    run.reply(asked, Plan(...))                # the agent answered
    run.fail(asked2, FailureReason.TIMED_OUT)
    run.advance_to(30.0)
    run.stop()
    assert run.finished is not None            # the framework adopted and finished

``FlowRun.start(..., restarting=True)`` round-trips the state through JSON into a new driver
after every step, so any story can be repeated as a crash-at-every-step test; a
``RestartPolicy`` instead restarts after the steps it names, and with ``changes`` each of its
restarts is also a code change (``code_changed`` makes a changed definition), so every flow
restarts from its latest checkpoint. ``FakeView`` is the ``RunView`` the search reads: set the
clock, attempts and settlements on it.

``FAKE_OPERATIONS`` and ``fake_search`` are the namespace and the decorator a test search uses;
``Prompt`` is the context its renders carry, so a turn request can be told by its prompt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, ClassVar, Literal, TypedDict, Unpack

from vs_core.api import (
    AdoptionFailed,
    AdoptionFailureReason,
    AdoptionFinished,
    AdoptionResult,
    ArtifactId,
    ArtifactRef,
    AttemptRef,
    CheckpointKnown,
    Continuation,
    ContinuationId,
    ContinuationPhase,
    ControlId,
    ControlInput,
    DecisionId,
    Event,
    EventId,
    EvidenceReadingsBase,
    Failed,
    FailureReason,
    Finish,
    FlowState,
    InterpretEvidenceBase,
    InvocationId,
    InvocationRef,
    ObservationStatus,
    Operation,
    OperationFinished,
    OperationId,
    OperationResult,
    ProposeWinner,
    RejectionCode,
    RenderedArtifacts,
    RenderRoleArtifactsBase,
    RequestId,
    RequestTurn,
    ResumeAuthorized,
    ResumeFailed,
    ResumeReady,
    ResumeRefusal,
    ResumeRefused,
    Scope,
    Settlement,
    SettlementId,
    StartAttempt,
    StopLifted,
    StopRequested,
    StrategyOperations,
    TimeReached,
    TurnEnded,
    TurnResult,
    TurnYielded,
    Value,
    VerifyParentRevisionBase,
    Wait,
    Waiting,
    WakeAt,
    Withdraw,
    Work,
    WorkspaceClosed,
    WorkspaceOpened,
    encode_json,
    run_view,
)
from vs_core.flows import search
from vs_core.testing.builders import initial_state
from vs_core.types.common import Observation, StopMode
from vs_core.types.sessions import TurnFailureKind

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

    from vs_core.api import (
        Decision,
        Flow,
        NoWinner,
        OperationRequest,
        Role,
        RunFacts,
        RunTrace,
        RunView,
        SearchDef,
        Seconds,
        SpanKind,
        Within,
    )
    from vs_core.flows._define import Root
    from vs_core.types.agents import Checkpoint


class Prompt(Value):
    """What a fake renderer is asked to render: the text a test tells prompts apart by."""

    text: str


class Render(RenderRoleArtifactsBase[Prompt]):
    """Render one role prompt."""

    kind: Literal["fake.render_role_artifacts"] = "fake.render_role_artifacts"


class Verify(VerifyParentRevisionBase):
    """Verify a parent revision."""

    kind: Literal["fake.verify_parent_revision"] = "fake.verify_parent_revision"


class Readings(EvidenceReadingsBase[Value]):
    """No readings: the fake namespace never interprets evidence."""


class Interpret(InterpretEvidenceBase):
    """Decode evidence."""

    kind: Literal["fake.interpret_evidence"] = "fake.interpret_evidence"
    outcome_model: ClassVar[type[BaseModel]] = Readings


FAKE_OPERATIONS = StrategyOperations("fake", Render, Verify, Interpret)
FAKE_REGISTRY = FAKE_OPERATIONS.registry()


def fake_search[C](
    name: str,
    config: type[C],
    roles: tuple[Role, ...] = (),
    spans: tuple[SpanKind, ...] = (),
    *,
    snapshot: type[Any] | None = None,
) -> Callable[[Root[C]], SearchDef[C]]:
    """``@search`` over the fake namespace: renders are answered by ``FlowRun``."""
    return search(
        name,
        config=config,
        operations=FAKE_OPERATIONS,
        roles=roles,
        spans=spans,
        snapshot=snapshot,
    )


def code_changed[C](definition: SearchDef[C]) -> SearchDef[C]:
    """``definition`` with one more journaled read where its root starts: changed code.

    Its journal and the original's diverge at their first step, as they do after a change that
    alters a prompt or a decision: a run resumed under the other one restarts every flow from
    its latest checkpoint. Changing back is a code change again.
    """
    root = definition.root

    async def changed(ctx: Flow, start: C) -> NoWinner | None:
        ctx.clock.now()
        return await root(ctx, start)

    return replace(definition, root=changed)


@dataclass
class FakeView:
    """The run view a search reads. Change the fields between steps; ``current`` is the view."""

    now_at: Seconds = 0.0
    changes: dict[str, object] = field(default_factory=dict)
    """Other ``RunView`` fields to override: ``attempts``, ``settlements``, ``capabilities``..."""

    def current(self) -> RunView:
        """The view as it stands now."""
        base = run_view(initial_state())
        return base.model_copy(update={"now_at": self.now_at, **self.changes})


@dataclass(frozen=True)
class RestartPolicy:
    """After which steps a run restarts: every step, or only those in ``steps``.

    A step is one event the search handled, counted from 0 at the first.
    """

    steps: frozenset[int] = frozenset()
    every: bool = False
    after_stop: bool = False
    """Also restart after every step once the run was told to stop."""
    changes: tuple[SearchDef[Any], ...] = ()
    """When given, every restart is also a code change: the run goes on under the next of these
    definitions, in turn (``(code_changed(search), search)`` alternates two codes)."""

    def after(self, step: int, *, stopped: bool) -> bool:
        """Whether the run restarts after its ``step``-th event, once told to stop or not."""
        return self.every or step in self.steps or (self.after_stop and stopped)


class StartOptions(TypedDict, total=False):
    """What a story may set when it starts a run."""

    view: FakeView | None
    restarting: bool | RestartPolicy
    adopting: bool
    facts: RunFacts


@dataclass(frozen=True)
class Asked:
    """One wait the search opened: its decision, the request and its limit."""

    decision: DecisionId
    request: Work | Finish
    until: Within | None
    """The limit of a wait; None for a ``Finish``, which core answers only by refusing it."""
    context: Value | None = None
    """For a turn request: the context its prompt was rendered from."""
    template: str | None = None
    """For a turn request: the template its prompt was rendered with."""

    @property
    def prompt(self) -> Prompt | None:
        """The search's own prompt of a turn request, or None for a prompt core started."""
        return self.context if isinstance(self.context, Prompt) else None

    @property
    def label(self) -> str:
        """The id root the framework gave: ``a/worker.2`` was issued as ``worker``."""
        return self.decision.root.rpartition("/")[2].rpartition(".")[0]

    @property
    def path(self) -> str:
        """The flow that asked: empty for the root, ``a/b/`` for task ``b`` of task ``a``."""
        head, slash, _ = self.decision.root.rpartition("/")
        return f"{head}{slash}"


class FlowRun[C]:
    """A flow search being played by a story. See the module docstring."""

    def __init__(
        self,
        search: SearchDef[C],
        config: C,
        *,
        view: FakeView | None = None,
        restarting: bool | RestartPolicy = False,
        adopting: bool = True,
    ) -> None:
        """Build a run that has not started; ``start`` is the way in."""
        self.definition = search
        self.config = config
        self.registry = search.registry() or FAKE_REGISTRY
        """The codec of the namespace the search declared: its operations are answered through it."""
        self.driver = search.driver(config, self.registry)
        self.view = view or FakeView()
        self.restarting = restarting
        self._steps = 0
        self._stopped = False
        self.adopting = adopting
        """Whether the host adopts any proposed winner at once, as the real one does."""
        self.state: FlowState
        self.decisions: list[Decision] = []
        """Every decision the search issued, in order."""
        self._open: list[Asked] = []
        self._sleeps: list[Seconds] = []
        self._prompts: dict[ArtifactId, tuple[str, Value]] = {}
        self._owners: dict[str, Callable[[OperationRequest], BaseModel]] = {}
        self._queue: list[Event] = []
        self.finished: Finish | None = None
        """The ``Finish`` the search issued, once it did."""
        self.restarts = 0
        self._timeline: list[str] = []
        self._sequence = 0

    @classmethod
    def start(cls, search: SearchDef[C], config: C, **options: Unpack[StartOptions]) -> FlowRun[C]:
        """Start the search and take its first decisions.

        ``options`` are those of ``StartOptions``: the run's ``facts`` (a search that reads
        ``ctx.task`` needs them to carry its task terms), the ``view`` it reads, whether the host
        is ``restarting`` after every step and whether it ``adopting`` any winner at once.
        """
        facts = options.get("facts")
        run = cls(
            search,
            config,
            view=options.get("view"),
            restarting=options.get("restarting", False),
            adopting=options.get("adopting", True),
        )
        run.state, issued = run.driver.init(
            config, initial_state().run.facts if facts is None else facts, run.view.current()
        )
        run._take(issued)
        run._pump()
        return run

    # -- what is open ------------------------------------------------------------------------

    @property
    def open(self) -> tuple[Asked, ...]:
        """The waits not yet answered, in the order they were opened."""
        return tuple(self._open)

    @property
    def sleeping_until(self) -> tuple[Seconds, ...]:
        """The wake-up times asked for and not yet reached."""
        return tuple(self._sleeps)

    def waiting(self) -> tuple[Waiting, ...]:
        """What each suspended flow waits for, as data: its path and its sources."""
        return self.driver.waiting(self.state)

    def trace(self) -> RunTrace:
        """The run's spans: every flow task and awaited operation so far."""
        return self.driver.trace(self.state)

    def timeline(self) -> str:
        """The run so far as one line: ``worker.1:Plan ws-1/work.1:failed(timed-out) stop t=30``."""
        return " ".join(self._timeline)

    def expect(self, kind: type[Work | Finish], *, label: str | None = None) -> Asked:
        """The first open wait for a ``kind`` request (with this id root, if given)."""
        for asked in self._open:
            if isinstance(asked.request, kind) and label in (None, asked.label):
                return asked
        wanted = f"{kind.__name__} {label or ''}".strip()
        have = [f"{type(a.request).__name__} {a.decision.root}" for a in self._open]
        message = f"no open wait for {wanted}; open: {have}"
        raise TypeError(message)

    def expect_turn(self, text: str | None = None, *, role: str | None = None) -> Asked:
        """The first open turn request, of this role and asked with a prompt of this text."""
        for asked in self._open:
            if (
                isinstance(asked.request, RequestTurn)
                and role in (None, asked.label)
                and (text is None or (asked.prompt is not None and asked.prompt.text == text))
            ):
                return asked
        have = [f"{a.decision.root}:{a.prompt.text if a.prompt else ''}" for a in self._open]
        message = f"no open turn for {role or ''} {text or ''}; open: {have}"
        raise TypeError(message)

    # -- delivering events -------------------------------------------------------------------

    def reply(
        self, asked: Asked, reply: BaseModel, *, checkpoint: Checkpoint | None = None
    ) -> None:
        """The agent answered a turn request with ``reply``.

        ``checkpoint`` is core's outcome for the revision the turn produced, which it reports
        after the turn ended; a reply that asks for one waits for it.
        """
        self._close(asked)
        self._timeline.append(f"{asked.decision.root}:{type(reply).__name__}")
        ended = self._ended(asked, ObservationStatus.SUCCEEDED, output=reply.model_dump_json())
        self.send(ended)
        if checkpoint is not None:
            self.send(CheckpointKnown(invocation=ended.result.invocation, checkpoint=checkpoint))

    def suspend(self, asked: Asked, handles: tuple[str, ...] = ("h",)) -> Continuation:
        """The agent ended the turn waiting for evaluations and core recorded the wait.

        ``resume_ready`` then lets core authorize the resume, as the evaluations settling does.
        """
        self._close(asked)
        self._timeline.append(f"{asked.decision.root}:suspended")
        waiting = json.dumps({"kind": "waiting_for_evaluation", "handles": list(handles)})
        ended = self._ended(asked, ObservationStatus.SUCCEEDED, output=waiting, suspending=True)
        current = ended.result.invocation
        continuation = Continuation(
            continuation_id=ContinuationId(root=f"{current.invocation_id.root}/evaluation"),
            invocation=current,
            next_invocation=current.model_copy(
                update={"invocation_id": InvocationId(root=f"{current.invocation_id.root}/resume")}
            ),
            jobs=(),
            deadline_at=self.view.now_at + 1000.0,
            phase=ContinuationPhase.WAITING,
        )
        self.send(ended)
        self.send(TurnYielded(continuation=continuation))
        return continuation

    def resume_ready(self, continuation: Continuation) -> None:
        """Core authorized the resume of a suspended turn: the evaluations it waited for settled."""
        self._timeline.append("resume-ready")
        authorization = ResumeAuthorized(
            continuation_id=continuation.continuation_id,
            next_invocation=continuation.next_invocation,
            evidence=(),
        )
        self.send(ResumeReady(authorization=authorization))

    def resume_refused(self, continuation: Continuation, reason: ResumeRefusal) -> None:
        """Core will never resume a suspended turn: the history its evaluations left is unusable."""
        self._timeline.append("resume-refused")
        refusal = ResumeRefused(
            continuation_id=continuation.continuation_id,
            invocation=continuation.invocation,
            reason=reason,
        )
        self.send(ResumeFailed(refusal=refusal))

    def reply_raw(self, asked: Asked, output_json: str) -> None:
        """The agent answered with text that may not be its reply model (a correction follows)."""
        self._close(asked)
        self._timeline.append(f"{asked.decision.root}:raw")
        self.send(self._ended(asked, ObservationStatus.SUCCEEDED, output=output_json))

    def lose(self, asked: Asked) -> None:
        """The transport lost the turn."""
        self._close(asked)
        self._timeline.append(f"{asked.decision.root}:lost")
        self.send(
            self._ended(asked, ObservationStatus.FAILED, failure=TurnFailureKind.TRANSPORT_LOST)
        )

    def fail_turn(self, asked: Asked, failure: TurnFailureKind) -> None:
        """The provider failed the turn for good."""
        self._close(asked)
        self._timeline.append(f"{asked.decision.root}:turn-failed")
        self.send(self._ended(asked, ObservationStatus.FAILED, failure=failure))

    def fail(self, asked: Asked, reason: FailureReason, **fields: object) -> None:
        """Deliver ``Failed(reason)`` for a wait; ``fields`` complete the reason's own fields."""
        defaults: dict[str, object] = {}
        if reason is FailureReason.TIMED_OUT:
            defaults["limit_at"] = self.view.now_at
        if reason is FailureReason.REFUSED:
            defaults["cause"] = RejectionCode.BUDGET
        self._close(asked)
        self._timeline.append(f"{asked.decision.root}:failed({reason.value})")
        self.send(Failed(reason=reason, decision=asked.decision, **{**defaults, **fields}))  # ty: ignore[invalid-argument-type]

    def open_workspace(self, asked: Asked, generation: int = 0) -> AttemptRef:
        """The host opened the workspace a ``StartAttempt`` asked for."""
        request = asked.request
        if not isinstance(request, StartAttempt):
            message = f"{asked.decision.root} is not a StartAttempt"
            raise TypeError(message)
        ref = AttemptRef(attempt_id=request.attempt_id, generation=generation)
        self._close(asked)
        self._timeline.append(f"{asked.decision.root}:opened")
        self.send(WorkspaceOpened(decision=asked.decision, attempt=ref))
        return ref

    def close_workspace(self, asked: Asked, *, candidate_revision: object = None) -> Settlement:
        """The host settled the workspace a ``Withdraw`` asked to settle, as it was asked to."""
        request = asked.request
        if not isinstance(request, Withdraw) or not isinstance(request.target, AttemptRef):
            message = f"{asked.decision.root} is not a Withdraw of an attempt"
            raise TypeError(message)
        disposition = request.disposition
        if disposition.kind != "settle":
            message = "only a settling Withdraw is closed here"
            raise TypeError(message)
        self._sequence += 1
        settlement = Settlement(
            settlement_id=SettlementId(root=f"settlement-{self._sequence}"),
            attempt=request.target,
            candidate=disposition.candidate if candidate_revision is None else candidate_revision,  # ty: ignore[invalid-argument-type]
            assessments=disposition.assessments,
            eligible=disposition.eligible,
            retention=disposition.retention,
            outcome=disposition.outcome,
        )
        self._close(asked)
        self._timeline.append(f"{asked.decision.root}:closed")
        self.send(WorkspaceClosed(decision=asked.decision, settlement=settlement))
        return settlement

    def adopt(self, asked: Asked) -> None:
        """The host adopted the winner a ``ProposeWinner`` named (when ``adopting`` is off)."""
        if not isinstance(asked.request, ProposeWinner):
            message = f"{asked.decision.root} is not a ProposeWinner"
            raise TypeError(message)
        self._close(asked)
        self._timeline.append(f"{asked.decision.root}:adopted")
        self.send(self._adopted(asked.request))

    def refuse_adoption(self, asked: Asked, reason: AdoptionFailureReason) -> None:
        """The host could not adopt the winner a ``ProposeWinner`` named."""
        if not isinstance(asked.request, ProposeWinner):
            message = f"{asked.decision.root} is not a ProposeWinner"
            raise TypeError(message)
        self._close(asked)
        self._timeline.append(f"{asked.decision.root}:adoption-failed({reason.value})")
        self.send(
            AdoptionFinished(
                decision=None,
                result=AdoptionFailed(
                    selection=asked.request.selection,
                    reason=reason,
                    observation=self._observation(ObservationStatus.FAILED),
                    rounds=1,
                ),
            )
        )

    def answer(self, asked: Asked, event: Event) -> None:
        """Deliver any event as the answer to a wait (a measurement, an operation result)."""
        self._close(asked)
        self._timeline.append(f"{asked.decision.root}:{event.kind}")
        self.send(event)

    def finish_operation(self, asked: Asked, outcome: BaseModel) -> None:
        """An operation the search asked for ended with ``outcome`` (verify, interpret evidence)."""
        request = asked.request
        if not isinstance(request, Operation):
            message = f"{asked.decision.root} is not an operation"
            raise TypeError(message)
        self._sequence += 1
        result = self._operation_result(request.request, outcome)
        self.answer(asked, OperationFinished(decision=asked.decision, result=result))

    def own(self, kind: str, perform: Callable[[OperationRequest], BaseModel]) -> None:
        """Answer every operation of ``kind`` at once with ``perform``'s outcome, from now on.

        For an owned operation a story is not about: its waits never show among the open ones.
        Any such wait already open is answered now.
        """
        self._owners[kind] = perform
        for asked in [item for item in self._open if self._owned(item.request)]:
            self.finish_operation(asked, self._perform(asked.request))

    def published[M: BaseModel](self, model: type[M]) -> M | None:
        """What the flow last published with ``ctx.publish``, as ``model``."""
        return self.driver.published(self.state, model)

    def stop(self, control: str = "stop", mode: StopMode = StopMode.DRAIN) -> None:
        """The operator (or the host, at the deadline) asks the run to stop, draining or not."""
        self._timeline.append(control)
        self.send(
            StopRequested(
                control=ControlInput(control_id=ControlId(root=control), action="stop", mode=mode)
            )
        )

    def lift_stop(self, control: str = "resume") -> None:
        """The operator resumes the run, which lifts the stop that stood."""
        self._timeline.append(control)
        self.send(
            StopLifted(
                control=ControlInput(
                    control_id=ControlId(root=control), action="resume", when="now"
                )
            )
        )

    def advance_to(self, time: Seconds) -> None:
        """Move the clock to ``time`` and deliver ``TimeReached`` for it."""
        self.view.now_at = time
        self._timeline.append(f"t={time:g}")
        self._sleeps = [t for t in self._sleeps if t > time]
        self.send(TimeReached(time=time))

    def send(self, event: Event) -> None:
        """Deliver an event as it is (then what the host answers by itself)."""
        self._queue.append(event)
        self._pump()

    def restart(self) -> None:
        """Round-trip the state through JSON and continue with a new driver.

        Under a ``RestartPolicy`` with ``changes`` the new driver runs the next changed code.
        """
        self.restarts += 1
        self.state = FlowState.model_validate_json(encode_json(self.state))
        changes = self.restarting.changes if isinstance(self.restarting, RestartPolicy) else ()
        if changes:
            self.swap(changes[(self.restarts - 1) % len(changes)])
        else:
            self.driver = self.driver.fresh()

    def swap(self, search: SearchDef[C]) -> None:
        """Continue the run under another definition of the search: the code changed on resume."""
        self.definition = search
        self.registry = search.registry() or FAKE_REGISTRY
        self.driver = search.driver(self.config, self.registry)

    # -- bookkeeping -------------------------------------------------------------------------

    def _pump(self) -> None:
        while self._queue:
            event = self._queue.pop(0)
            self.state, issued = self.driver.update(self.state, event, self.view.current())
            self._take(issued)
            self._stopped = self._stopped or isinstance(event, StopRequested)
            policy = RestartPolicy(every=True) if self.restarting is True else self.restarting
            if policy and policy.after(self._steps, stopped=self._stopped):
                self.restart()
            self._steps += 1

    def _take(self, issued: list[Decision]) -> None:
        self.decisions.extend(issued)
        for decision in issued:
            match decision:
                case Wait(request=Operation(request=render) as operation) if self._renders(render):
                    self._render(operation, render)  # ty: ignore[invalid-argument-type]
                case Wait(request=Operation() as operation) if self._owned(operation):
                    self._sequence += 1
                    result = self._operation_result(operation.request, self._perform(operation))
                    self._queue.append(
                        OperationFinished(decision=operation.decision_id, result=result)
                    )
                case Wait(request=ProposeWinner() as proposal) if self.adopting:
                    self._open.append(Asked(proposal.decision_id, proposal, None))
                    self._queue.append(self._adopted(proposal))
                    self._close(self._open[-1])
                case Wait(request=request, until=until):
                    template, context = self._prompt(request)
                    self._open.append(Asked(request.decision_id, request, until, context, template))
                case WakeAt(time=time):
                    self._sleeps.append(time)
                case Finish():
                    self.finished = decision
                    self._open.append(Asked(decision.decision_id, decision, None))

    def _prompt(self, request: Work) -> tuple[str | None, Value | None]:
        if isinstance(request, RequestTurn) and request.turn.prompts:
            found = self._prompts.get(request.turn.prompts[0].artifact_id)
            if found is not None:
                return found
        return None, None

    def _owned(self, request: object) -> bool:
        return isinstance(request, Operation) and request.request.kind in self._owners

    def _perform(self, request: object) -> BaseModel:
        if not isinstance(request, Operation):
            message = f"{type(request).__name__} is not an operation"
            raise TypeError(message)
        return self._owners[request.request.kind](request.request)

    def _renders(self, request: object) -> bool:
        """Whether ``request`` asks to render a prompt: the namespace's render operation."""
        operations = self.definition.operations
        return operations is not None and isinstance(request, operations.render)

    def _render(self, operation: Operation, render: RenderRoleArtifactsBase[Value]) -> None:
        """Answer a prompt render at once: the artifact stands for the prompt it was made from."""
        self._sequence += 1
        artifact = ArtifactId(root=f"prompt-{self._sequence}")
        self._prompts[artifact] = (render.template, render.context)
        rendered = RenderedArtifacts(
            status="succeeded", prompts=(ArtifactRef(artifact_id=artifact, digest="d"),)
        )
        self._queue.append(
            OperationFinished(
                decision=operation.decision_id, result=self._operation_result(render, rendered)
            )
        )

    def _operation_result(self, request: object, outcome: BaseModel) -> OperationResult:
        encoded = self.registry.encode(request)  # ty: ignore[invalid-argument-type]
        result = OperationResult(
            operation_id=OperationId(root=f"operation-{self._sequence}"),
            observation=self._observation(ObservationStatus.SUCCEEDED),
            outcome_schema=encoded.schema_ref.outcome_schema,
            operation_schema=encoded.schema_ref,
            outcome=outcome,
        )
        return self.registry.validate_event(result)

    def _observation(self, status: ObservationStatus) -> Observation:
        self._sequence += 1
        return Observation(
            event_id=EventId(root=f"event-{self._sequence}"),
            request_id=RequestId(root=f"request-{self._sequence}"),
            scope=Scope(owner=initial_state().run.run_id, generation=0),
            sequence=1,
            observed_at=self.view.now_at,
            status=status,
            accepted=True,
            terminal=True,
            released=True,
        )

    def _adopted(self, proposal: ProposeWinner) -> Event:
        return AdoptionFinished(
            decision=None,
            result=AdoptionResult(
                selection=proposal.selection,
                observation=self._observation(ObservationStatus.SUCCEEDED),
            ),
        )

    def _ended(
        self,
        asked: Asked,
        status: ObservationStatus,
        *,
        output: str | None = None,
        failure: TurnFailureKind | None = None,
        suspending: bool = False,
    ) -> TurnEnded:
        request = asked.request
        if not isinstance(request, RequestTurn):
            message = f"{asked.decision.root} is not a turn request"
            raise TypeError(message)
        spec = request.turn
        scope = request.scope if request.scope.owner else initial_state().run.run_id
        invocation = InvocationRef(
            session_id=spec.session.session_id,
            invocation_id=spec.invocation_id,
            generation=getattr(scope, "generation", 0),
        )
        return TurnEnded(
            decision=asked.decision,
            result=TurnResult(
                invocation=invocation,
                observation=self._observation(status),
                output_schema=spec.output_schema,
                output_json=output,
                failure=failure,
                suspending=suspending,
            ),
        )

    def _close(self, asked: Asked) -> None:
        if asked in self._open:
            self._open.remove(asked)
