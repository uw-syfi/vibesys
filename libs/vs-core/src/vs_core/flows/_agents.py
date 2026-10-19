"""``ctx.agents``: a session with an agent, and the turn mechanics core owns.

``ctx.agents.session(role, workspace=ws)`` is a conversation: the same session object continues
it, a new one starts fresh. ``await session.turn(prompt, within=...)`` asks the agent once and
gets one ``TurnFinished`` or one ``Failed``. Between the two, core runs the turn:

    render the prompt -> run the turn -> validate the reply against the role's model
    -> ask for a correction of an invalid reply -> ask again after a lost turn (after a backoff)
    -> suspend a turn that waits for evaluations and resume it when core authorizes
    -> hold a write turn's reply until core recorded its checkpoint -> deliver ONE result.

This is ordinary flow code over the runtime's own handle (a ``Kit``), so it is journaled and
replayed like any flow: a restart after any step lands the turn in the same place. Every rule
of the ask is the role's (``Role``): how long a turn may take, how often core asks again on its
own and after what wait, which failures it corrects, how long a prompt may render. A role with
no corrections and no lost turns gets ``Failed(INVALID_REPLY)`` (or ``Failed(LOST)``) at once
and asks again itself. What stays here is mechanism: every wait is bounded, and a resume names
its continuation, which core authorizes once.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING

from pydantic import TypeAdapter, ValidationError

from vs_core._registry import ContractError
from vs_core._strategy_operations import RenderedArtifacts
from vs_core._waitable import WaitableReply, WaitingForDependency
from vs_core.types.agents import (
    ChargeClass,
    Checkpoint,
    Correctable,
    Correction,
    ProtocolContext,
    ReplyCorrection,
    ResumeFacts,
    Retained,
    WaitUnrecorded,
)
from vs_core.types.common import (
    THIS_RUN,
    DecisionId,
    InvocationRef,
    ObservationStatus,
    RevisionRef,
    Scope,
    Seconds,
    Value,
    WorkspaceMode,
    WorkspaceRef,
)
from vs_core.types.job_observations import WaitEnd
from vs_core.types.search import (
    CheckpointKnown,
    OperationFinished,
    ResumeFailed,
    ResumeReady,
    TurnEnded,
    TurnFinished,
    TurnYielded,
    YieldFailed,
)
from vs_core.types.sessions import SessionSpec, TurnFailureKind, TurnResult, TurnSpec
from vs_core.types.strategy import Operation, RequestTurn
from vs_core.types.until import (
    Failed,
    FailureReason,
    WaitRefusal,
    Within,
)

from ._errors import TimeUp
from ._roles import reply_errors
from ._sources import First

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic import BaseModel

    from vs_core.types.search import Event

    from ._kit import Kit
    from ._labels import Label
    from ._roles import Role
    from ._sources import Signal
    from ._workspaces import Workspace


_LIFECYCLE = TypeAdapter(bool)


def _digest(text: str) -> str:
    return sha256(" ".join(text.split()).encode()).hexdigest()


class _Again:
    """The turn must be asked again: the state of the ask says how."""


_AGAIN = _Again()
_BACKOFF_STEPS = 16
"""Outage losses past this many wait the backoff's cap; the doubling stops before it overflows."""


class Agents:
    """The agent aspect of ``ctx``."""

    def __init__(self, kit: Kit) -> None:
        """Bind the aspect to its flow."""
        self._kit = kit

    def session(
        self,
        role: Role,
        *,
        workspace: Workspace | None = None,
        revision: RevisionRef | None = None,
        started_by: Sequence[str] = (),
    ) -> Session:
        """A new conversation with ``role``, in ``workspace`` (the run when omitted).

        ``revision`` is the revision of the role's workspace for its turns, unless a turn names
        another; it is needed for a role that works on a revision. ``started_by`` names the
        agents (each a session's ``agent``) whose work started this one; none when the search
        itself did. The role must be one the
        search declares (``@search(roles=...)``); the declared role, with the model the operator
        chose for this run, is the one asked.
        """
        declared = self._kit.run.wiring.roles.get(role.name)
        if declared is None:
            message = f"role {role.name!r} is not declared by this search: list it in roles="
            raise ContractError(("agents", "session"), message)
        if declared.model_copy(update={"model": role.model}) != role:
            message = f"role {role.name!r} differs from the one the search declared"
            raise ContractError(("agents", "session"), message)
        role = declared
        number = self._kit.count(f"session:{role.name}")
        subject = self._kit.path.rstrip("/") or "run"
        if number > 1:
            subject = f"{subject}#{number}"
        return Session(self._kit, role, subject, _Opened(workspace, revision, tuple(started_by)))


@dataclass(frozen=True)
class _Opened:
    """What a session is opened with: its workspace, its revision, and the agents that started it."""

    workspace: Workspace | None
    revision: RevisionRef | None
    started_by: tuple[str, ...]


class Session:
    """One conversation with one role. Turns of the same session continue it."""

    def __init__(
        self,
        kit: Kit,
        role: Role,
        subject: str,
        opened: _Opened,
    ) -> None:
        """Make the session; ``ctx.agents.session`` is the way in."""
        self._kit = kit
        self.role = role
        self.subject = subject
        self.workspace = opened.workspace
        self.revision = opened.revision
        self.started_by = opened.started_by
        """The agents whose work started this one; empty when the search itself did."""

    @property
    def agent(self) -> str:
        """The agent's stable identity in the run: the id of the conversation its turns continue.

        The same across replay and restart: the id scheme derives it from the role and the
        subject (the flow's path and the session's number among the role's sessions there).
        """
        ids = self._kit.run.wiring.ids
        if ids is None:
            message = "this search was registered without an id scheme: it cannot ask agents"
            raise ContractError(("agents", "session"), message)
        return ids.session_id(self.role.name, self.subject).root

    async def turn(
        self,
        prompt: Value,
        *,
        within: Seconds | Within | None = None,
        revision: RevisionRef | None = None,
        label: Label | None = None,
    ) -> TurnFinished | Failed:
        """Ask the agent once: its validated reply, or the typed failure that ended the turn.

        ``prompt`` is the context the role's template renders; a ``Correction`` names the
        earlier turn it corrects. Each provider turn of the ask may take the role's
        ``turn_time``; ``within`` narrows that for this ask (the smaller limit applies) and
        adds the watches that may end it earlier. ``label`` labels the span of each provider
        turn the ask makes.
        """
        asked = (
            None
            if within is None
            else (within if isinstance(within, Within) else Within(limit=within))
        )
        role = self.role
        limit = role.turn_time if asked is None else min(asked.limit, role.turn_time)
        until = Within(limit=limit, watches=() if asked is None else asked.watches)
        ask = _Ask(self._kit, self, prompt, _Terms(until, revision or self.revision, label))
        while True:
            step = await ask.attempt()
            if not isinstance(step, _Again):
                return step


@dataclass(frozen=True)
class _Terms:
    """What one ask was asked under: its bound, the revision it works on, its spans' label."""

    until: Within
    revision: RevisionRef | None
    label: Label | None


class _Ask:
    """One ask: what stays the same across its corrections, re-asks and resumes, and what changes."""

    def __init__(self, kit: Kit, session: Session, prompt: Value, terms: _Terms) -> None:
        self.kit = kit
        until, revision, self.label = terms.until, terms.revision, terms.label
        self.session = session
        self.role = session.role
        self.until = until
        self.revision = revision
        self.input: Value = prompt
        follows = prompt.of if isinstance(prompt, Correction) else None
        self.charge: ChargeClass = "correction" if follows is not None else self.role.charge.value
        self.template = self.role.prompt
        """The template the next ask renders: the role's prompt, or one of its exchanges."""
        self.predecessor: InvocationRef | None = follows
        self.resume: InvocationRef | None = None
        self.continuation = None
        self.corrections = 0
        self.drops = 0
        self.outages = 0
        """Turns of this ask core ended while the capacity gate held them; never a limit."""
        self.resuming = False
        """Whether the ask in flight continues a wait: a loss of it is never asked again, unless
        its provider lost it (``_unavailable``)."""
        self.invocation: InvocationRef | None = None
        self.decision = DecisionId(root=self.role.name)

    # -- one attempt ------------------------------------------------------------------------

    async def attempt(self) -> TurnFinished | Failed | _Again:
        """Render, run and judge one provider turn of the ask."""
        kit = self.kit
        serial = kit.count(f"serial:{self.role.name}:{self.session.subject}") - 1
        rendered = await self._render(serial)
        if isinstance(rendered, Failed):
            return rendered
        invocation, request = self._build(rendered, serial)
        yielded = kit.signal(
            f"yield of {invocation.invocation_id.root}",
            lambda event: (
                (isinstance(event, TurnYielded) and event.continuation.invocation == invocation)
                or (isinstance(event, YieldFailed) and event.failure.invocation == invocation)
            ),
        )
        try:
            bound = Within(limit=self.until.limit, watches=self.until.watches)
            pending = kit.start(request, bound, self.label)
            self.decision = pending.decision
            event = await kit.settle(pending)
            if isinstance(event, Failed):
                return event
            if not isinstance(event, TurnEnded):
                message = f"a turn ended with {event.kind}"
                raise ContractError(("agents", "turn"), message)
            return await self._ended(event.result, yielded)
        finally:
            kit.run.release(yielded)

    async def _render(self, serial: int) -> RenderedArtifacts | Failed:
        operations = self.kit.run.wiring.operations
        if operations is None:
            message = "this search was registered without operations: it cannot ask agents"
            raise ContractError(("agents", "turn"), message)
        # The namespace's render class pins ``kind`` by default; the declared base type cannot say so.
        request = operations.render(  # ty: ignore[missing-argument]
            template=self.template,
            subject=f"{self.session.subject}.{self.role.name}",
            ordinal=serial,
            context=self.input,
        )
        scope: Scope = THIS_RUN if self.session.workspace is None else self.session.workspace.scope
        operation = Operation(
            decision_id=DecisionId(root=f"{self.role.name}.render"), scope=scope, request=request
        )
        event = await self.kit.wait(
            operation, Within(limit=self.role.render_time, watches=self.until.watches)
        )
        match event:
            case Failed():
                return event
            case OperationFinished(result=result):
                outcome = result.outcome
                if (
                    isinstance(outcome, RenderedArtifacts)
                    and outcome.status == "succeeded"
                    and outcome.prompts
                ):
                    return outcome
                return Failed(reason=FailureReason.RENDER_FAILED, decision=self.decision)
            case unexpected:
                message = f"a render ended with {unexpected.kind}"
                raise ContractError(("agents", "turn"), message)

    def _build(self, rendered: RenderedArtifacts, serial: int) -> tuple[InvocationRef, RequestTurn]:
        """The kernel turn for a rendered prompt."""
        kit, role = self.kit, self.role
        ids = kit.run.wiring.ids
        if ids is None:
            message = "this search was registered without an id scheme: it cannot ask agents"
            raise ContractError(("agents", "turn"), message)
        workspace = self.session.workspace
        scope = kit.read(lambda view: view.run, Scope) if workspace is None else workspace.scope
        subject = self.session.subject
        session = SessionSpec(
            session_id=ids.session_id(role.name, subject),
            role_id=ids.role_id(role.identity),
            policy="reuse",
            lifetime="owner",
            access=role.access,
            model=role.model,
        )
        invocation_id = (
            self.resume.invocation_id
            if self.resume is not None
            else ids.invocation_id(role.name, subject, serial)
        )
        invocation = InvocationRef(
            session_id=session.session_id,
            invocation_id=invocation_id,
            generation=scope.generation,
        )
        where: WorkspaceRef | Scope
        if role.workspace is None:
            where = scope
        elif self.revision is None:
            message = f"{role.name} works on a revision: name it on the session or the turn"
            raise ContractError(("agents", "turn"), message)
        else:
            # A writing role works in its workspace as core admitted it (isolated, or the run's
            # own root); a reading role reads the revision as declared.
            mode = role.workspace
            if workspace is not None and role.workspace is not WorkspaceMode.READ_ONLY_REVISION:
                mode = workspace.mode
            where = WorkspaceRef(scope=scope, revision=self.revision, mode=mode)
        spec = TurnSpec(
            session=session,
            invocation_id=invocation_id,
            continuation_id=self.continuation,
            workspace=where,
            prompts=rendered.prompts,
            output_schema=role.reply_schema,
            tool_policy=rendered.tool_policy,
            charge_class=self.charge,
            # A resume names its continuation, never a predecessor: core rejects both together.
            predecessor=None if self.charge == "resume" else self.predecessor,
        )
        request = RequestTurn(decision_id=DecisionId(root=role.name), scope=scope, turn=spec)
        return invocation, request

    # -- what the turn came to -----------------------------------------------------------------

    async def _ended(self, result: TurnResult, yielded: Signal) -> TurnFinished | Failed | _Again:
        role = self.role
        turn = result.invocation
        output = result.output_json
        if result.observation.status is not ObservationStatus.SUCCEEDED or output is None:
            return await self._unsuccessful(result)
        self.invocation = turn
        if result.suspending:
            # Core recorded the wait: the reply is dropped and the agent answers when resumed.
            offered = self.kit.read(lambda view: "suspend" in view.capabilities.lifecycle, bool)
            if not offered or not role.may_wait_for_evaluations:
                return Failed(
                    reason=FailureReason.WAIT_REFUSED,
                    decision=self.decision,
                    turn=turn,
                    refusal=WaitRefusal.NOT_OFFERED,
                )
            return await self._suspended(yielded)
        try:
            parsed = role.wire_reply.model_validate_json(output)
        except ValidationError as error:
            errors = reply_errors(error)
            invalid = Failed(
                reason=FailureReason.INVALID_REPLY, decision=self.decision, errors=errors, turn=turn
            )
            return self._corrected(
                Correctable.INVALID_REPLY,
                role.exchanges.correction,
                ReplyCorrection(role=role.name, errors=errors),
                invalid,
            )
        reply = parsed.reply if isinstance(parsed, WaitableReply) else parsed
        if isinstance(reply, WaitingForDependency):
            refused = Failed(
                reason=FailureReason.WAIT_REFUSED,
                decision=self.decision,
                turn=turn,
                refusal=WaitRefusal.NOT_RECORDED,
            )
            return self._corrected(
                Correctable.UNRECORDED_WAIT,
                role.exchanges.wait_unrecorded,
                WaitUnrecorded(role=role.name),
                refused,
            )
        return await self._replied(turn, reply)

    async def _replied(self, turn: InvocationRef, reply: BaseModel) -> TurnFinished | Failed:
        """A valid reply: held until core's checkpoint is known when the role needs one."""
        role = self.role
        if not role.reply_needs_checkpoint(reply):
            return self._finished(turn, reply, None)
        known = self.kit.run.checkpoints.get(turn)
        if known is None:
            found = await self._checkpoint(turn)
            if isinstance(found, Failed):
                return found
            known = found
        return self._finished(turn, reply, known)

    def _finished(
        self, turn: InvocationRef, reply: object, checkpoint: Checkpoint | None
    ) -> TurnFinished:
        return TurnFinished(
            decision=self.decision,
            invocation=turn,
            reply=reply,  # ty: ignore[invalid-argument-type]
            checkpoint=checkpoint,
        )

    async def _unsuccessful(self, result: TurnResult) -> Failed | _Again:
        role, turn = self.role, result.invocation
        if result.failure is TurnFailureKind.TRANSPORT_LOST:
            return await self._lost(turn)
        if result.failure is TurnFailureKind.PROVIDER_UNAVAILABLE:
            return await self._unavailable(turn)
        if result.reply_errors:
            # The executor's own schema check rejected the reply: the same typed failure as
            # a reply that breaks the model here, with the errors it reported by field.
            invalid = Failed(
                reason=FailureReason.INVALID_REPLY,
                decision=self.decision,
                errors=result.reply_errors,
                turn=turn,
            )
            self.invocation = turn
            return self._corrected(
                Correctable.INVALID_REPLY,
                role.exchanges.correction,
                ReplyCorrection(role=role.name, errors=result.reply_errors),
                invalid,
            )
        return Failed(
            reason=FailureReason.TURN_FAILED,
            decision=self.decision,
            turn=turn,
            turn_failure=result.failure or TurnFailureKind.PROVIDER_FAILED,
            digest=_digest(result.detail),
        )

    def _corrected(
        self,
        kind: Correctable,
        template: str | None,
        context: ProtocolContext,
        exhausted: Failed,
    ) -> _Again | Failed:
        """Ask the agent to fix its reply, or report ``exhausted``.

        Core asks only for a failure the role lists as correctable, and only while the role's
        corrections are not spent. ``template`` renders ``context`` as the correction prompt.
        """
        role = self.role
        if kind not in role.correctable or self.corrections >= role.corrections:
            return exhausted
        if template is None:
            message = f"{role.name} is corrected for {kind.value} but declares no such exchange"
            raise ContractError(("agents", "turn"), message)
        self.input = context
        self.template = template
        self.charge = "correction"
        self.predecessor = self.invocation
        self.resume = None
        self.continuation = None
        self.corrections += 1
        self.drops = 0
        self.resuming = False
        return _AGAIN

    async def _lost(self, lost: InvocationRef) -> Failed | _Again:
        """The transport lost the turn: ask the same question again after a backoff, or report it.

        A turn that continued a wait is never asked again, whatever the role's ``lost_turns``:
        core authorizes each continuation's resume once, so the caller decides what follows.
        """
        role = self.role
        allowed = 0 if self.resuming else role.lost_turns
        if self.drops >= allowed:
            return Failed(reason=FailureReason.LOST, decision=self.decision, turn=lost)
        await self.kit.timer(self.kit.now() + role.lost_turn_backoff.delay(self.drops))
        self.charge = "correction"
        self.predecessor = lost
        self.resume = None
        self.continuation = None
        self.invocation = None
        self.drops += 1
        return _AGAIN

    async def _unavailable(self, lost: InvocationRef) -> _Again:
        """The provider lost the turn while the run's capacity gate held it: ask again.

        The turn's own work never failed; its provider was unavailable (an outage, a spent
        quota) past the time core gave it. That is not the role's to spend, so it never counts
        against ``lost_turns``, and a turn that continued a wait is asked again too: the new
        turn follows the lost one in its conversation, with the same prompt, as a correction
        (its continuation was used). The gate's own budget, and the pause it puts the run in
        once that is spent, bound how long the provider is waited for.
        """
        role = self.role
        await self.kit.timer(
            self.kit.now() + role.lost_turn_backoff.delay(min(self.outages, _BACKOFF_STEPS))
        )
        self.charge = "correction"
        self.predecessor = lost
        self.resume = None
        self.continuation = None
        self.invocation = None
        self.resuming = False
        self.outages += 1
        return _AGAIN

    # -- waiting for what core decides ---------------------------------------------------------

    async def _bounded(self, signal: Signal) -> Event | Failed:
        """The next event of ``signal``, or ``TIMED_OUT`` if the run's deadline comes first."""
        deadline = self.kit.read(lambda view: view.deadline_at, float)
        got = await First((signal, self.kit.timer(deadline)))
        if isinstance(got, TimeUp):
            return Failed(reason=FailureReason.TIMED_OUT, decision=self.decision, limit_at=got.at)
        return got

    async def _suspended(self, yielded: Signal) -> Failed | _Again:
        """The turn ended with a wait: wait for core to record it, then to authorize the resume."""
        turn = self.invocation
        if turn is None:
            message = "a suspended turn has run"
            raise ContractError(("agents", "turn"), message)
        recorded = await self._bounded(yielded)
        if isinstance(recorded, Failed):
            return recorded
        if isinstance(recorded, YieldFailed):
            return Failed(
                reason=FailureReason.WAIT_REFUSED,
                decision=self.decision,
                turn=turn,
                refusal=WaitRefusal.NOT_PUBLISHED,
            )
        if not isinstance(recorded, TurnYielded):
            message = f"a suspension was recorded as {recorded.kind}"
            raise ContractError(("agents", "turn"), message)
        wanted = recorded.continuation.continuation_id
        ready = self.kit.signal(
            f"resume of {wanted.root}",
            lambda event: (
                (isinstance(event, ResumeReady) and event.authorization.continuation_id == wanted)
                or (isinstance(event, ResumeFailed) and event.refusal.continuation_id == wanted)
            ),
        )
        try:
            authorized = await self._bounded(ready)
        finally:
            self.kit.run.release(ready)
        if isinstance(authorized, Failed):
            return authorized
        if isinstance(authorized, ResumeFailed):
            return Failed(
                reason=FailureReason.WAIT_REFUSED,
                decision=self.decision,
                turn=turn,
                refusal=WaitRefusal.NOT_RESUMABLE,
            )
        if not isinstance(authorized, ResumeReady):
            message = f"a resume was authorized as {authorized.kind}"
            raise ContractError(("agents", "turn"), message)
        return self._resumed(turn, authorized)

    def _resumed(self, turn: InvocationRef, ready: ResumeReady) -> _Again:
        """Core authorized the resume of a suspended turn: ask it to continue."""
        authorization = ready.authorization
        known = self.kit.run.checkpoints.get(turn)
        timeout = authorization.timeout
        facts = ResumeFacts(
            agent=self.role.name,
            retained=known.revision if isinstance(known, Retained) else None,
            ended_by=timeout.by if timeout is not None else None,
            queue_budget_spent=timeout is not None
            and any(job.by is WaitEnd.QUEUE_BUDGET for job in timeout.unfinished),
            evidence=authorization.evidence,
            repeated_failure=authorization.repeated_failure,
            comparisons=authorization.comparisons,
            settling=authorization.settling,
        )
        template = self.role.exchanges.resume
        if template is None:
            message = f"{self.role.name} waited for evaluations but declares no resume exchange"
            raise ContractError(("agents", "turn"), message)
        self.input = facts
        self.template = template
        self.resuming = True
        self.charge = "resume"
        self.predecessor = None
        self.resume = authorization.next_invocation
        self.continuation = authorization.continuation_id
        self.drops = 0
        return _AGAIN

    async def _checkpoint(self, turn: InvocationRef) -> Checkpoint | Failed:
        """Hold a write turn's reply until core recorded or declined its checkpoint."""
        known = self.kit.signal(
            f"checkpoint of {turn.invocation_id.root}",
            lambda event: isinstance(event, CheckpointKnown) and event.invocation == turn,
        )
        try:
            got = await self._bounded(known)
        finally:
            self.kit.run.release(known)
        if isinstance(got, Failed):
            return got
        if not isinstance(got, CheckpointKnown):
            message = f"a checkpoint was reported as {got.kind}"
            raise ContractError(("agents", "turn"), message)
        return got.checkpoint


__all__ = ["Agents", "Session", "reply_errors"]
