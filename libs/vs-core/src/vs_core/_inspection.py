"""Correlate inspection target facts with canonical intent and resource ownership."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._proofs import Proven, descriptor_matches, invocation_for, observation_for, operation_for
from ._registry import ContractError
from .types.common import LifecycleClass, OperationNormalizationKind
from .types.evaluation import InspectOwnedJob
from .types.intents import ExecuteRegisteredOperation, InspectRequest
from .types.sessions import DispatchTurn, InspectTurn, ResumeSessionTurn

if TYPE_CHECKING:
    from .types.common import ResourceId
    from .types.intents import Intent, RequestObserved, TargetObservation
    from .types.kernel import CoreState


def _owned_resource(state: CoreState, original: Intent, resource: ResourceId) -> bool:
    """Resource identity must already belong to this exact submission or manifest."""
    return resource in _root_resources(state, original) | _descendants(state, original)


def _validate_invocation(state: CoreState, query: InspectTurn, original: Intent) -> None:
    """Session generation comes from durable invocation ownership, never inference."""
    request = original.request
    proof = invocation_for(state.sessions.invocations, query.invocation, original.request.scope)
    if not isinstance(proof, Proven):
        raise ContractError(("target", "invocation"), "does not match canonical invocation owner")
    invocation = proof.value
    if isinstance(request, DispatchTurn | ResumeSessionTurn):
        matched = invocation.turn == request.turn
    elif isinstance(request, ExecuteRegisteredOperation):
        operation = operation_for(state.run.receipts, request)
        descriptor = descriptor_matches(
            state.registry,
            state.run.capabilities,
            request.operation,
            LifecycleClass.SESSION_TURN,
            OperationNormalizationKind.NONE,
        )
        matched = (
            isinstance(operation, Proven)
            and isinstance(descriptor, Proven)
            and original.lifecycle == LifecycleClass.SESSION_TURN
            and operation.value.registered_turn == invocation.turn
            and invocation.registered_operation == request.operation_id
        )
    else:
        matched = False
    if not matched:
        raise ContractError(("target", "invocation"), "does not match canonical invocation owner")


def _root_resources(state: CoreState, original: Intent) -> set[ResourceId]:
    """Only canonical root identities can carry the original request's outcome."""
    resources = {
        job.resource_id
        for job, submission in (
            *((job, job.submission_id) for job in state.evaluation.jobs),
            *((job, job.request_id) for job in state.evaluation.registered_jobs),
        )
        if submission == original.request_id
        and job.scope == original.request.scope
        and job.resource_id is not None
    }
    if (
        original.observation is not None
        and isinstance(observation_for(original, original.observation), Proven)
        and original.observation.resource_id is not None
    ):
        resources.add(original.observation.resource_id)
    return resources


def _descendants(state: CoreState, original: Intent) -> set[ResourceId]:
    """Provisional roots still own every discovered descendant manifest."""
    resources = {
        child.resource_id
        for child in state.intents.children
        if child.scope == original.request.scope and original.request_id in child.source_requests
    }
    resources.update(
        resource
        for job, submission in (
            *((job, job.submission_id) for job in state.evaluation.jobs),
            *((job, job.request_id) for job in state.evaluation.registered_jobs),
        )
        if submission == original.request_id and job.scope == original.request.scope
        for resource in job.children
    )
    observations = tuple(
        job.observation
        for job, submission in (
            *((job, job.submission_id) for job in state.evaluation.jobs),
            *((job, job.request_id) for job in state.evaluation.registered_jobs),
        )
        if submission == original.request_id
        and job.scope == original.request.scope
        and job.observation is not None
    )
    if original.observation is not None:
        observations = (*observations, original.observation)
    resources.update(
        resource
        for observation in observations
        if observation.request_id == original.request_id
        and observation.scope == original.request.scope
        for resource in observation.children
    )
    return resources


def _validate_child(state: CoreState, target: TargetObservation, original: Intent) -> None:
    resource = target.target_resource
    roots = _root_resources(state, original)
    observed = target.observation.resource_id
    if resource is not None:
        if (
            observed != resource
            or resource in roots
            or not _owned_resource(state, original, resource)
        ):
            raise ContractError(("target", "resource_id"), "child lacks canonical ownership proof")
    elif observed is not None and roots and observed not in roots:
        raise ContractError(("target", "resource_id"), "root facts name a different owned resource")
    elif observed is not None and observed in _descendants(state, original):
        raise ContractError(
            ("target", "target_resource"), "descendant facts require the child discriminator"
        )


def validate_registered_owner(state: CoreState, event: RequestObserved) -> None:
    """Registry proof cannot substitute a different canonical operation descriptor."""
    targets = ((event, ("outcome",)),)
    if event.target is not None:
        targets = (*targets, (event.target, ("target", "outcome")))
    for observed, path in targets:
        original = next(
            (
                row
                for row in state.intents.intents
                if row.request_id == observed.observation.request_id
            ),
            None,
        )
        if original is not None:
            proof = observation_for(original, observed.observation)
            if not isinstance(proof, Proven):
                field = (
                    "scope"
                    if observed.observation.scope != original.request.scope
                    else "admission_id"
                )
                raise ContractError(
                    (*path, field), "differs from canonical request scope or episode"
                )
        if observed.operation_schema is None:
            continue
        if (
            original is None
            or not isinstance(original.request, ExecuteRegisteredOperation)
            or observed.operation_schema != original.request.operation.schema_ref
        ):
            raise ContractError(
                path, "registered outcome differs from canonical operation descriptor"
            )


def _validate_query(
    state: CoreState,
    request: InspectRequest | InspectTurn | InspectOwnedJob,
    target: TargetObservation,
    original: Intent,
) -> None:
    """Each inspection class names its canonical target identity."""
    if isinstance(request, InspectRequest):
        if request.target != original.request_id:
            raise ContractError(("target", "request_id"), "does not match inspected request")
        if request.resource_id != target.target_resource:
            raise ContractError(("target", "resource_id"), "does not match inspected child")
    elif isinstance(request, InspectTurn):
        if target.target_resource is not None:
            raise ContractError(("target", "resource_id"), "turn inspection requires root facts")
        _validate_invocation(state, request, original)
    elif request.resource_id != target.observation.resource_id or not _owned_resource(
        state, original, request.resource_id
    ):
        raise ContractError(("target", "resource_id"), "does not match canonical job owner")


def validate_inspection_target(state: CoreState, event: RequestObserved) -> None:
    """Reject uncorrelated target facts before any inspection or lifecycle mutation."""
    target = event.target
    if target is None:
        return
    query = next(
        (row for row in state.intents.intents if row.request_id == event.observation.request_id),
        None,
    )
    original = next(
        (row for row in state.intents.intents if row.request_id == target.observation.request_id),
        None,
    )
    if query is None or not isinstance(
        query.request, InspectRequest | InspectTurn | InspectOwnedJob
    ):
        raise ContractError(("target",), "target facts require a recorded inspection request")
    if original is None or not isinstance(observation_for(original, target.observation), Proven):
        raise ContractError(
            ("target", "scope"), "target requires its canonical request scope and episode"
        )
    if (
        not isinstance(observation_for(query, event.observation), Proven)
        or query.request.scope != original.request.scope
    ):
        raise ContractError(("target", "scope"), "inspection and target scope or episode differs")
    _validate_query(state, query.request, target, original)
    _validate_child(state, target, original)


__all__ = ["validate_inspection_target", "validate_registered_owner"]
