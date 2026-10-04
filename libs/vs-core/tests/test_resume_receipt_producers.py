"""Composed history, publication and dispatch regressions through the public API."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .test_continuations import fixture, parked_fixture, reopened_fixture, roundtrip
from .test_registered_v3_contracts import RegisteredMeasurement, measurement_codec
from .test_resume_dispatch_proof import digest


def publication_state(*, facts: bool = True) -> tuple[core.CoreState, core.Continuation]:
    state, wait = fixture(settled=True)
    owner = state.attempts.attempts[0]
    predecessor = state.sessions.invocations[0]
    assert owner.admission_id is not None
    start = core.StartAttempt(
        decision_id=owner.admission_id,
        scope=core.Scope(owner=state.run.run_id, generation=owner.generation),
        attempt_id=owner.attempt_id,
        item_id=owner.item_id,
        workspace=owner.workspace,
        budget=owner.budget,
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "receipts": (
                        core.DecisionReceipt(
                            decision_id=start.decision_id,
                            decision=start,
                            feedback=core.Accepted(decision_id=start.decision_id),
                            payload_digest=digest(start),
                        ),
                    )
                }
            )
        }
    )
    # Capture at the paid-cycle boundary, before any submission exists. Attempts
    # charging remains a stub; the published area API exposes Sessions' producer.
    idle = state.sessions.model_copy(
        update={
            "invocations": (),
            "sessions": (
                state.sessions.sessions[0].model_copy(
                    update={"phase": core.SessionPhase.IDLE, "invocation": None}
                ),
            ),
        }
    )
    captured = core.advance_session(
        idle,
        core.SessionsContext(
            run=state.run,
            attempts=state.attempts,
            evaluation=core.EvaluationState(),
            intents=core.IntentsState(),
        ),
        core.TurnRequested(scope=predecessor.scope, turn=predecessor.turn),
    )
    prefix = captured.state.invocations[0].evaluation_prefix
    assert prefix == core.EvaluationHistoryCursor()
    predecessor = predecessor.model_copy(update={"evaluation_prefix": prefix})
    sources = []
    budgets = []
    owned_jobs = []
    for index, original_job in enumerate(state.evaluation.jobs):
        job = original_job.model_copy(
            update={
                "plan": original_job.plan.model_copy(
                    update={
                        "recipe": original_job.plan.recipe.model_copy(
                            update={"digest": f"recipe-{index}"}
                        )
                    }
                )
            }
        )
        owned_jobs.append(job)
        assert job.observation is not None
        request = core.SubmitMeasurement(
            request_id=job.submission_id,
            scope=job.scope,
            deadline_at=100.0,
            plan=job.plan,
        )
        result = (
            core.EvaluationTerminalFacts(
                stages=(
                    core.EvaluationStageResult(
                        stage_id="measure", outcome=core.EvaluationStageOutcome.PASSED
                    ),
                ),
                accuracy_passed=True,
            )
            if facts
            else None
        )
        sources.append(
            core.Intent(
                request_id=job.submission_id,
                request=request,
                payload_digest=digest(request),
                lifecycle=core.LifecycleClass.OWNED_JOB,
                phase=core.IntentPhase.COMPLETED,
                observation=job.observation,
                evaluation_result=result,
                reconcile_deadline_at=100.0,
            )
        )
        identity = core.MeasurementIdentity(
            purpose=job.plan.purpose,
            candidate=state.run.facts.baseline,
            evaluator_digest=job.plan.evaluator_digest,
            workload_digest=job.plan.workload_digest,
            environment_digest=job.plan.environment_digest,
            recipe_digest=job.plan.recipe.digest,
            stages=(core.MeasurementStageIdentity(stage_id="measure"),),
        )
        budgets.append(
            core.SubmissionBudget(
                scope=job.scope,
                identity=identity,
                limit=job.plan.submission_limit,
                receipts=(core.PreparedSubmissionReceipt(request_id=job.submission_id, ordinal=1),),
            )
        )
    return state.model_copy(
        update={
            "sessions": state.sessions.model_copy(update={"invocations": (predecessor,)}),
            "evaluation": state.evaluation.model_copy(
                update={"jobs": tuple(owned_jobs), "submission_budgets": tuple(budgets)}
            ),
            "intents": state.intents.model_copy(
                update={"intents": (*state.intents.intents, *sources)}
            ),
        }
    ), wait


def dispatch_publication(state: core.CoreState, wait: core.Continuation) -> core.Transition:
    predecessor = state.sessions.invocations[0]
    turn = predecessor.turn.model_copy(
        update={
            "invocation_id": wait.next_invocation.invocation_id,
            "continuation_id": wait.continuation_id,
            "predecessor": wait.invocation,
            "charge_class": "resume",
        }
    )
    decision = core.RequestTurn(
        decision_id=core.DecisionId(root="accepted-resume"),
        scope=predecessor.scope,
        turn=turn,
    )
    request = core.ResumeSessionTurn(
        request_id=core.RequestId(root="resume-dispatch"),
        decision_id=decision.decision_id,
        admission_id=state.attempts.attempts[0].admission_id,
        scope=predecessor.scope,
        deadline_at=turn.deadline_at,
        turn=turn,
        continuation_id=wait.continuation_id,
    )
    assert request.request_id is not None
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        feedback=core.Accepted(decision_id=decision.decision_id),
        payload_digest=digest(decision),
        request_ids=(request.request_id,),
    )
    intent = core.Intent(
        request_id=request.request_id,
        request=request,
        payload_digest=digest(request),
        lifecycle=core.LifecycleClass.SESSION_TURN,
        phase=core.IntentPhase.PREPARED,
        reconcile_deadline_at=100.0,
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(update={"receipts": (*state.run.receipts, receipt)}),
            "intents": state.intents.model_copy(
                update={"intents": (*state.intents.intents, intent)}
            ),
        }
    )
    event = core.DispatchAuthorized(request_id=request.request_id)
    # Intents A dispatch remains an explicit stub. The kernel consumes the real
    # Evaluation/Sessions/Attempts producers before this public dispatch fence.
    return core.trace_step(
        state,
        event,
        core.ReducerTrace(
            frames=(core.TraceFrame(signal=event, change=core.IntentsChange(state=state.intents)),)
        ),
    )


def test_actual_suspension_publication_survives_restart_and_authorizes_dispatch() -> None:
    state, wait = publication_state()
    published = core.step(state, core.TurnSuspended(continuation=wait))
    feedback = published.events[0]
    assert isinstance(feedback, core.ResumeAuthorized)
    stored = published.state.evaluation.continuations[0]
    assert stored.authorization_receipt is not None
    assert stored.preceding_submission is None
    assert stored.authorization_receipt.history_cursor == feedback.history_cursor
    assert feedback.history_cursor == core.EvaluationHistoryCursor(
        ordinal=3, submission_id=state.evaluation.jobs[-1].submission_id
    )
    history = published.state.attempts.attempts[0].evaluation_history
    assert history.availability == core.EvaluationHistoryAvailability.COMPLETE
    assert len(history.records) == 3
    recovered = roundtrip(published.state)
    assert (
        recovered.evaluation.continuations[0].authorization_receipt == stored.authorization_receipt
    )
    assert dispatch_publication(recovered, wait).events == ()
    assert core.step(recovered, core.TurnSuspended(continuation=wait)).events == ()


@pytest.mark.parametrize("proof", ["exact", "missing", "unavailable", "duplicate"])
@given(ordinal=st.integers(min_value=1, max_value=3))
def test_second_yield_captures_previous_publication_separately_from_paid_cycle(
    proof: str,
    ordinal: int,
) -> None:
    state, first = publication_state()
    state = state.model_copy(
        update={
            "evaluation": state.evaluation.model_copy(
                update={
                    "jobs": state.evaluation.jobs[:ordinal],
                    "submission_budgets": state.evaluation.submission_budgets[:ordinal],
                }
            ),
            "intents": state.intents.model_copy(
                update={"intents": state.intents.intents[: ordinal + 1]}
            ),
        }
    )
    first = first.model_copy(update={"jobs": first.jobs[:ordinal]})
    published = core.step(state, core.TurnSuspended(continuation=first))
    state = published.state
    previous = state.evaluation.continuations[0]
    receipt = previous.authorization_receipt
    assert receipt is not None
    assert receipt.history_cursor is not None
    original = state.sessions.invocations[0]
    turn = original.turn.model_copy(
        update={
            "invocation_id": first.next_invocation.invocation_id,
            "continuation_id": first.continuation_id,
            "predecessor": first.invocation,
            "charge_class": "resume",
        }
    )
    assert original.observation is not None
    observation = original.observation.model_copy(
        update={"request_id": core.RequestId(root="resumed-yield")}
    )
    current = original.model_copy(
        update={"invocation": first.next_invocation, "turn": turn, "observation": observation}
    )
    request = core.ResumeSessionTurn(
        request_id=observation.request_id,
        scope=original.scope,
        admission_id=observation.admission_id,
        deadline_at=100.0,
        turn=turn,
        continuation_id=first.continuation_id,
    )
    source = core.Intent(
        request_id=observation.request_id,
        request=request,
        payload_digest=digest(request),
        lifecycle=core.LifecycleClass.SESSION_TURN,
        phase=core.IntentPhase.COMPLETED,
        observation=observation,
        reconcile_deadline_at=100.0,
    )
    owner = state.attempts.attempts[0]
    checkpoint = owner.checkpoints[0].model_copy(update={"invocation": current.invocation})
    charge = core.ChargeReceipt(
        charge_id=core.ChargeId(root="resumed-turn"),
        kind=core.ChargeKind.TURN,
        invocation_id=current.invocation.invocation_id,
        charged=1,
    )
    previous_rows = (previous,)
    if proof == "missing":
        previous_rows = (previous.model_copy(update={"authorization_receipt": None}),)
    elif proof == "unavailable":
        previous_rows = (
            previous.model_copy(
                update={
                    "authorization_receipt": receipt.model_copy(update={"history_cursor": None})
                }
            ),
        )
    elif proof == "duplicate":
        duplicate_id = core.ContinuationId(root="duplicate-publication")
        previous_rows = (
            previous,
            previous.model_copy(
                update={
                    "continuation_id": duplicate_id,
                    "authorization_receipt": receipt.model_copy(
                        update={"continuation_id": duplicate_id}
                    ),
                }
            ),
        )
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(
                attempts=(
                    owner.model_copy(
                        update={
                            "checkpoints": (*owner.checkpoints, checkpoint),
                            "charges": (*owner.charges, charge),
                        }
                    ),
                )
            ),
            "sessions": state.sessions.model_copy(
                update={
                    "invocations": (*state.sessions.invocations, current),
                    "sessions": (
                        state.sessions.sessions[0].model_copy(
                            update={"invocation": current.invocation.invocation_id}
                        ),
                    ),
                }
            ),
            "intents": state.intents.model_copy(
                update={"intents": (*state.intents.intents, source)}
            ),
            "evaluation": state.evaluation.model_copy(update={"continuations": previous_rows}),
        }
    )
    second = first.model_copy(
        update={
            "continuation_id": core.ContinuationId(root="second-wait"),
            "invocation": current.invocation,
            "next_invocation": current.invocation.model_copy(
                update={"invocation_id": core.InvocationId(root="second-resume")}
            ),
        }
    )
    result = core.step(state, core.TurnSuspended(continuation=second))
    stored = result.state.evaluation.continuations[-1]
    assert current.evaluation_prefix == core.EvaluationHistoryCursor()
    assert stored.preceding_submission == (receipt.history_cursor if proof == "exact" else None)
    if proof == "exact":
        assert stored.preceding_submission != current.evaluation_prefix
    assert (
        roundtrip(result.state).evaluation.continuations[-1].preceding_submission
        == stored.preceding_submission
    )


@given(cursor=st.one_of(st.none(), st.just(core.EvaluationHistoryCursor())))
def test_fresh_suspension_cannot_import_a_caller_supplied_publication_receipt(
    cursor: core.EvaluationHistoryCursor | None,
) -> None:
    state, wait = fixture(run_owned=True, settled=True)
    receipt = core.ResumeAuthorizationReceipt(
        continuation_id=wait.continuation_id,
        next_invocation=wait.next_invocation,
        evidence=(),
        history_cursor=cursor,
    )
    wait = wait.model_copy(update={"authorization_receipt": receipt})
    with pytest.raises(core.ContractError, match="only waiting intent"):
        core.step(state, core.TurnSuspended(continuation=wait))


@given(ordinal=st.integers(min_value=0, max_value=3))
def test_fresh_suspension_cannot_import_a_caller_supplied_previous_publication(
    ordinal: int,
) -> None:
    state, wait = fixture(settled=True)
    cursor = core.EvaluationHistoryCursor(
        ordinal=ordinal,
        submission_id=core.RequestId(root="caller-submission") if ordinal else None,
    )
    wait = wait.model_copy(update={"preceding_submission": cursor})
    with pytest.raises(core.ContractError, match="only waiting intent"):
        core.step(state, core.TurnSuspended(continuation=wait))


@given(facts=st.booleans(), missing=st.integers(min_value=-1, max_value=2))
def test_missing_scientific_or_submission_facts_never_certify_publication_history(
    *, facts: bool, missing: int
) -> None:
    state, wait = publication_state(facts=facts)
    if missing >= 0:
        sources = state.intents.intents
        state = state.model_copy(
            update={
                "intents": state.intents.model_copy(
                    update={
                        "intents": tuple(
                            row for index, row in enumerate(sources) if index != missing + 1
                        )
                    }
                )
            }
        )
    result = core.step(state, core.TurnSuspended(continuation=wait))
    receipt = result.state.evaluation.continuations[0].authorization_receipt
    assert receipt is not None
    if facts and missing == -1:
        assert receipt.history_cursor is not None
        dispatch_publication(result.state, wait)
    else:
        assert receipt.history_cursor is None
        with pytest.raises(core.ContractError, match="complete unexhausted"):
            dispatch_publication(result.state, wait)


@pytest.mark.parametrize(
    "fault", ["plan", "observation", "stage", "budget", "identity", "duplicate"]
)
@given(index=st.integers(min_value=0, max_value=2))
def test_history_certification_requires_exact_submission_identity_and_budget(
    fault: str, index: int
) -> None:
    state, wait = publication_state()
    sources = list(state.intents.intents)
    source = sources[index + 1]
    request = source.request
    assert isinstance(request, core.SubmitMeasurement)
    if fault == "plan":
        sources[index + 1] = source.model_copy(
            update={
                "request": request.model_copy(
                    update={"plan": request.plan.model_copy(update={"purpose": "profile"})}
                )
            }
        )
    elif fault == "observation":
        assert source.observation is not None
        sources[index + 1] = source.model_copy(
            update={"observation": source.observation.model_copy(update={"sequence": 2})}
        )
    elif fault == "stage":
        assert source.evaluation_result is not None
        sources[index + 1] = source.model_copy(
            update={
                "evaluation_result": source.evaluation_result.model_copy(
                    update={
                        "stages": (
                            core.EvaluationStageResult(
                                stage_id="undeclared", outcome=core.EvaluationStageOutcome.PASSED
                            ),
                        )
                    }
                )
            }
        )
    budgets = list(state.evaluation.submission_budgets)
    if fault == "budget":
        budgets.pop(index)
    elif fault == "identity":
        budgets[index] = budgets[index].model_copy(
            update={
                "identity": budgets[index].identity.model_copy(update={"recipe_digest": "other"})
            }
        )
    elif fault == "duplicate":
        budgets.append(budgets[index])
    state = state.model_copy(
        update={
            "intents": state.intents.model_copy(update={"intents": tuple(sources)}),
            "evaluation": state.evaluation.model_copy(
                update={"submission_budgets": tuple(budgets)}
            ),
        }
    )
    result = core.step(state, core.TurnSuspended(continuation=wait))
    receipt = result.state.evaluation.continuations[0].authorization_receipt
    assert receipt is not None
    assert receipt.history_cursor is None
    with pytest.raises(core.ContractError, match="complete unexhausted"):
        dispatch_publication(result.state, wait)


@pytest.mark.parametrize(
    "fault", ["exact", "normalization", "missing", "declaration", "receipt-duplicate"]
)
@given(index=st.integers(min_value=0, max_value=2))
def test_registered_history_requires_declared_codec_bound_measurement_identity(
    fault: str, index: int
) -> None:
    state, wait = publication_state()
    codec = measurement_codec()
    sources = [state.intents.intents[0]]
    receipts = list(state.run.receipts)
    jobs = []
    budgets = list(state.evaluation.submission_budgets)
    for position, (job, budget) in enumerate(
        zip(state.evaluation.jobs, state.evaluation.submission_budgets, strict=True)
    ):
        decision = codec.validate_decision(
            core.Operation(
                decision_id=core.DecisionId(root=f"registered-{position}"),
                scope=job.scope,
                deadline_at=100.0,
                request=RegisteredMeasurement(identity=budget.identity),
            )
        )
        request = core.ExecuteRegisteredOperation(
            request_id=job.submission_id,
            scope=job.scope,
            deadline_at=100.0,
            decision_id=decision.decision_id,
            operation_id=core.OperationId(root=f"operation:{decision.decision_id.root}"),
            operation=codec.encode(decision.request),
            retry_limit=state.run.limits.max_retries,
        )
        source = state.intents.intents[position + 1]
        sources.append(
            source.model_copy(update={"request": request, "payload_digest": digest(request)})
        )
        if position == index and fault in ("normalization", "missing"):
            decision = decision.model_copy(
                update={
                    "normalized_measurement": (
                        budget.identity.model_copy(update={"recipe_digest": "forged"})
                        if fault == "normalization"
                        else None
                    )
                }
            )
            if fault == "normalization":
                assert decision.normalized_measurement is not None
                budgets[position] = budget.model_copy(
                    update={"identity": decision.normalized_measurement}
                )
        receipts.append(
            core.DecisionReceipt(
                decision_id=decision.decision_id,
                decision=decision,
                feedback=core.Accepted(decision_id=decision.decision_id),
                payload_digest=digest(decision),
                request_ids=(job.submission_id,),
            )
        )
        if position == index and fault == "receipt-duplicate":
            receipts.append(receipts[-1])
        jobs.append(
            core.RegisteredOwnedJob(
                operation_id=request.operation_id,
                request_id=job.submission_id,
                scope=job.scope,
                resource_pool=core.PoolId(root="jobs"),
                resource_id=job.resource_id,
                observation=job.observation,
                status=job.status,
                terminal=job.terminal,
                expected_measurement=(
                    decision.normalized_measurement
                    if fault == "normalization" and position == index
                    else budget.identity
                ),
            )
        )
    state = state.model_copy(
        update={
            "registry": codec.descriptors,
            "run": state.run.model_copy(
                update={
                    "receipts": tuple(receipts),
                    "capabilities": core.Capabilities(
                        operations=() if fault == "declaration" else codec.descriptors
                    ),
                }
            ),
            "intents": state.intents.model_copy(update={"intents": tuple(sources)}),
            "evaluation": state.evaluation.model_copy(
                update={
                    "jobs": (),
                    "registered_jobs": tuple(jobs),
                    "submission_budgets": tuple(budgets),
                }
            ),
        }
    )
    result = core.step(state, core.TurnSuspended(continuation=wait))
    receipt = result.state.evaluation.continuations[0].authorization_receipt
    assert receipt is not None
    if fault == "exact":
        assert receipt.history_cursor is not None
        dispatch_publication(result.state, wait)
    else:
        assert receipt.history_cursor is None
        with pytest.raises(core.ContractError, match="complete unexhausted"):
            dispatch_publication(result.state, wait)


@pytest.mark.parametrize("fault", ["exact", "missing", "foreign-run", "duplicate", "generation"])
@given(other=st.integers(min_value=1, max_value=100))
def test_initial_paid_cycle_prefix_requires_unique_current_run_admission(
    fault: str, other: int
) -> None:
    state, _ = publication_state()
    origin = state.run.receipts[0]
    assert isinstance(origin.decision, core.StartAttempt)
    decision = origin.decision
    if fault == "foreign-run":
        decision = decision.model_copy(
            update={"scope": decision.scope.model_copy(update={"owner": core.RunId(root="other")})}
        )
    elif fault == "generation":
        decision = decision.model_copy(
            update={"scope": decision.scope.model_copy(update={"generation": other})}
        )
    origin = origin.model_copy(update={"decision": decision, "payload_digest": digest(decision)})
    receipts = () if fault == "missing" else (origin, origin) if fault == "duplicate" else (origin,)
    run = state.run.model_copy(update={"receipts": receipts})
    predecessor = state.sessions.invocations[0]
    idle = state.sessions.model_copy(
        update={
            "invocations": (),
            "sessions": (
                state.sessions.sessions[0].model_copy(
                    update={"phase": core.SessionPhase.IDLE, "invocation": None}
                ),
            ),
        }
    )
    captured = core.advance_session(
        idle,
        core.SessionsContext(
            run=run,
            attempts=state.attempts,
            evaluation=core.EvaluationState(),
            intents=core.IntentsState(),
        ),
        core.TurnRequested(scope=predecessor.scope, turn=predecessor.turn),
    )
    prefix = captured.state.invocations[0].evaluation_prefix
    assert prefix == (core.EvaluationHistoryCursor() if fault == "exact" else None)


@pytest.mark.parametrize("exact", [False, True])
@given(index=st.integers(min_value=0, max_value=2), admission_present=st.booleans())
def test_history_correlates_source_admission_with_its_canonical_submission(
    index: int, *, exact: bool, admission_present: bool
) -> None:
    state, wait = publication_state()
    sources = list(state.intents.intents)
    source = sources[index + 1]
    assert source.observation is not None
    admission = state.attempts.attempts[0].admission_id if admission_present else None
    observation_admission = admission if exact else core.DecisionId(root="unrelated-admission")
    observed = source.observation.model_copy(update={"admission_id": observation_admission})
    sources[index + 1] = source.model_copy(
        update={
            "request": source.request.model_copy(update={"admission_id": admission}),
            "observation": observed,
        }
    )
    jobs = list(state.evaluation.jobs)
    jobs[index] = jobs[index].model_copy(update={"observation": observed})
    state = state.model_copy(
        update={
            "intents": state.intents.model_copy(update={"intents": tuple(sources)}),
            "evaluation": state.evaluation.model_copy(update={"jobs": tuple(jobs)}),
        }
    )
    result = core.step(state, core.TurnSuspended(continuation=wait))
    receipt = result.state.evaluation.continuations[0].authorization_receipt
    assert receipt is not None
    assert (receipt.history_cursor is not None) == exact
    if exact:
        assert dispatch_publication(result.state, wait).events == ()
    else:
        with pytest.raises(core.ContractError, match="complete unexhausted"):
            dispatch_publication(result.state, wait)


def test_published_receipt_survives_valid_parking_and_confirmed_reopen_without_republication() -> (
    None
):
    state, wait = fixture(settled=True)
    published = core.step(state, core.TurnSuspended(continuation=wait))
    receipt = published.state.evaluation.continuations[0].authorization_receipt
    assert receipt is not None
    parked, _ = parked_fixture()
    closing = parked.model_copy(
        update={
            "evaluation": parked.evaluation.model_copy(
                update={"continuations": published.state.evaluation.continuations}
            )
        }
    )
    authority = parked.evaluation.continuations[0].park_authority
    parked_result = core.step(
        closing,
        core.ContinuationRetireRequested(
            continuation_id=wait.continuation_id, disposition="park", park_authority=authority
        ),
    )
    assert parked_result.state.evaluation.continuations[0].authorization_receipt == receipt
    reopened, continuation, event = reopened_fixture()
    reopened = reopened.model_copy(
        update={
            "evaluation": reopened.evaluation.model_copy(
                update={
                    "continuations": (
                        continuation.model_copy(update={"authorization_receipt": receipt}),
                    )
                }
            )
        }
    )
    resumed = core.step(roundtrip(reopened), event)
    assert resumed.events == ()
    assert resumed.state.evaluation.continuations[0].phase == core.ContinuationPhase.AUTHORIZED
    assert resumed.state.evaluation.continuations[0].authorization_receipt == receipt
