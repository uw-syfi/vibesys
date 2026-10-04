"""Strict immutable sibling reducers for integrating Sessions A through public step.

These Fakes implement only the lifecycle events used by the session slice tests.
Unsupported events fail explicitly; they never silently accept missing ownership.
"""

import vs_core.api as core


def _invocation(state: core.SessionsState, ref: core.InvocationRef) -> core.Invocation:
    row = next((row for row in state.invocations if row.invocation == ref), None)
    if row is None:
        raise core.ContractValidationError("invocation", "unknown invocation")
    return row


def _input_proof(
    state: core.SessionsState,
    context: core.SessionsContext,
    event: core.InputAcceptanceObserved | core.InputReservationReleased,
) -> core.Invocation:
    invocation = _invocation(state, event.invocation)
    observation = event.observation
    intent = next(
        (row for row in context.intents.intents if row.request_id == observation.request_id), None
    )
    if (
        invocation.scope != observation.scope
        or invocation.observation is None
        or invocation.observation.request_id != observation.request_id
        or intent is None
        or intent.request.scope != invocation.scope
        or not isinstance(intent.request, core.DispatchTurn | core.ResumeSessionTurn)
        or intent.request.turn != invocation.turn
    ):
        raise core.ContractValidationError("observation", "input proof lacks exact dispatch")
    manifest = tuple(row.input_id for row in intent.request.inputs)
    records = tuple(row for row in state.inputs if row.reserved_to == event.invocation)
    if (
        manifest != invocation.input_ids
        or len(set(manifest)) != len(manifest)
        or {row.input.input_id for row in records} != set(manifest)
    ):
        raise core.ContractValidationError(
            "input_ids", "proof differs from exact reserved manifest"
        )
    return invocation


def fake_session_inputs(
    state: core.SessionsState, context: core.SessionsContext, event: core.SessionsEvent
) -> core.AreaChange[core.SessionsState]:
    match event:
        case core.InputAcceptanceObserved():
            invocation = _input_proof(state, context, event)
            if (
                not event.observation.accepted
                or event.observation.status == core.ObservationStatus.UNKNOWN
            ):
                raise core.ContractValidationError(
                    "observation.accepted", "delivery lacks acceptance"
                )
            records = tuple(
                record.model_copy(
                    update={
                        "receipt": core.InputDelivered(
                            input_id=record.input.input_id,
                            invocation=event.invocation,
                            observation=event.observation,
                        )
                    }
                )
                if record.reserved_to == event.invocation
                and record.receipt is None
                and record.input.input_id in invocation.input_ids
                else record
                for record in state.inputs
            )
            return core.AreaChange(state=state.model_copy(update={"inputs": records}))
        case core.InputReservationReleased():
            invocation = _input_proof(state, context, event)
            if (
                event.observation.accepted
                or not event.observation.terminal
                or event.observation.status
                not in (
                    core.ObservationStatus.REJECTED,
                    core.ObservationStatus.FAILED,
                    core.ObservationStatus.CANCELLED,
                )
            ):
                raise core.ContractValidationError(
                    "observation", "release lacks positive nonacceptance"
                )
            records = tuple(
                _released_record(record, event)
                if record.reserved_to == event.invocation
                and record.receipt is None
                and record.input.input_id in invocation.input_ids
                else record
                for record in state.inputs
            )
            return core.AreaChange(state=state.model_copy(update={"inputs": records}))
        case core.InvocationCheckpointAvailable():
            if any(row.invocation == event.invocation for row in state.interrupts):
                raise core.ContractValidationError(
                    "event", "Fake does not implement interruption refunds"
                )
            return core.AreaChange(state=state)
        case _:
            raise core.ContractValidationError(
                "event", f"unsupported Fake Inputs event {event.kind}"
            )


def _released_record(
    record: core.InputRecord, event: core.InputReservationReleased
) -> core.InputRecord:
    if isinstance(record.input.target, core.InvocationInputTarget):
        receipt = core.InputDropped(
            input_id=record.input.input_id,
            target=record.input.target,
            reason=core.InputDropReason.INVOCATION_TERMINAL,
            at=event.observation.observed_at,
        )
        return record.model_copy(update={"receipt": receipt})
    return record.model_copy(update={"reserved_to": None})


def _failed_checkpoint(
    state: core.AttemptsState, context: core.AttemptsContext, event: core.WorkspaceObserved
) -> core.AreaChange[core.AttemptsState]:
    owner = next(
        (
            row
            for row in state.attempts
            if row.attempt_id == event.attempt.attempt_id
            and row.generation == event.attempt.generation
        ),
        None,
    )
    intent = next(
        (row for row in context.intents.intents if row.request_id == event.observation.request_id),
        None,
    )
    scope = core.Scope(owner=event.attempt.attempt_id, generation=event.attempt.generation)
    if (
        owner is None
        or intent is None
        or not isinstance(intent.request, core.SnapshotAndRetain)
        or intent.request.attempt != event.attempt
        or intent.request.scope != scope
        or event.observation.scope != scope
        or intent.request.admission_id != owner.admission_id
        or event.observation.admission_id != owner.admission_id
        or intent.request_id not in owner.pending_intents
        or not event.observation.terminal
        or event.revision is not None
        or event.observation.status
        not in (
            core.ObservationStatus.FAILED,
            core.ObservationStatus.REJECTED,
            core.ObservationStatus.CANCELLED,
        )
    ):
        raise core.ContractValidationError(
            "observation", "failure lacks exact checkpoint authority"
        )
    blocked = owner.model_copy(update={"phase": core.AttemptPhase.BLOCKED})
    return core.AreaChange(
        state=state.model_copy(
            update={"attempts": tuple(blocked if row == owner else row for row in state.attempts)}
        )
    )


def fake_attempts(
    state: core.AttemptsState, context: core.AttemptsContext, event: core.AttemptsEvent
) -> core.AreaChange[core.AttemptsState]:
    if isinstance(event, core.WorkspaceObserved):
        return _failed_checkpoint(state, context, event)
    if not isinstance(event, core.InvocationEnded | core.InvocationCheckpointRequested):
        raise core.ContractValidationError("event", f"unsupported Fake Attempts event {event.kind}")
    owner = next(
        (
            row
            for row in state.attempts
            if row.attempt_id == event.attempt.attempt_id
            and row.generation == event.attempt.generation
        ),
        None,
    )
    invocation = _invocation(context.sessions, event.invocation)
    if (
        owner is None
        or invocation.scope
        != core.Scope(owner=event.attempt.attempt_id, generation=event.attempt.generation)
        or invocation.observation is None
        or not invocation.observation.terminal
    ):
        raise core.ContractValidationError(
            "invocation", "terminal proof lacks exact attempt ownership"
        )
    if isinstance(event, core.InvocationEnded):
        if event.observation != invocation.observation:
            raise core.ContractValidationError(
                "observation", "terminal proof differs from invocation"
            )
        return core.AreaChange(state=state)
    if event.authority != invocation.observation.request_id:
        raise core.ContractValidationError("authority", "checkpoint lacks exact terminal authority")
    request_id = core.RequestId(
        root=f"checkpoint:{len(event.authority.root)}:{event.authority.root}:"
        f"{len(event.invocation.invocation_id.root)}:{event.invocation.invocation_id.root}:{event.retention}"
    )
    request = core.SnapshotAndRetain(
        request_id=request_id,
        scope=invocation.scope,
        admission_id=owner.admission_id,
        deadline_at=min(context.run.deadline_at, invocation.turn.deadline_at),
        attempt=event.attempt,
        retention=event.retention,
        invocation=event.invocation,
    )
    if request_id in owner.pending_intents:
        return core.AreaChange(state=state)
    updated = owner.model_copy(update={"pending_intents": (*owner.pending_intents, request_id)})
    return core.AreaChange(
        state=state.model_copy(
            update={"attempts": tuple(updated if row == owner else row for row in state.attempts)}
        ),
        requests=(request,),
    )


def fake_evaluation(
    state: core.EvaluationState, context: core.EvaluationContext, event: core.EvaluationEvent
) -> core.AreaChange[core.EvaluationState]:
    if not isinstance(event, core.TurnSuspended):
        raise core.ContractValidationError(
            "event", f"unsupported Fake Evaluation event {event.kind}"
        )
    continuation = event.continuation
    invocation = _invocation(context.sessions, continuation.invocation)
    if (
        invocation.phase != core.SessionPhase.SUSPENDED
        or invocation.observation is None
        or not invocation.observation.accepted
        or not invocation.observation.terminal
        or continuation.phase != core.ContinuationPhase.WAITING
        or continuation.next_invocation == continuation.invocation
        or continuation.next_invocation.session_id != continuation.invocation.session_id
        or continuation.next_invocation.generation != continuation.invocation.generation
        or continuation.deadline_at > context.run.deadline_at
    ):
        raise core.ContractValidationError("continuation", "invalid exact yielded ownership")
    existing = next(
        (row for row in state.continuations if row.continuation_id == continuation.continuation_id),
        None,
    )
    if existing is not None:
        if existing != continuation:
            raise core.ContractValidationError("continuation", "identity payload conflict")
        return core.AreaChange(state=state)
    return core.AreaChange(
        state=state.model_copy(update={"continuations": (*state.continuations, continuation)})
    )
