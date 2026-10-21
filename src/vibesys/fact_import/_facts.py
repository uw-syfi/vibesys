"""Projection of validated values onto shared facts, with no scheduling continuity."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel

from vibesys.hypothesis import RoundRecord
from vs_core.api import (
    ChargeReceipt,
    ChildLease,
    EvidenceRef,
    InputDelivered,
    InputDropped,
    InputRecord,
    OwnedJob,
    RegisteredOwnedJob,
    RequestRecord,
    RunFacts,
    TaskTerms,
    values,
)
from vs_proto.api import (
    Oneof,
    core_agent_facts,
    core_common,
    core_contracts,
    core_control,
    core_leaves,
    core_vocabulary,
    validate,
)
from vs_proto.api import imported_facts as pb
from vs_runtime.api.core import EvidenceEntry
from vs_runtime.api.core_values import to_proto

from ._guidance import consolidate_guidance
from ._rounds import lower_round
from ._source import FactImportError, ImportSource, ensure_finite
from ._stage import stage_measurement

if TYPE_CHECKING:
    from collections.abc import Iterator


class FactSet:
    """Deduplicate immutable facts while preserving every source attribution."""

    def __init__(self) -> None:
        self._facts: dict[bytes, pb.ImportedFact] = {}
        self._identities: dict[tuple[str, str], bytes] = {}
        self._released_resources: set[str] = set()
        self._released_requests: set[str] = set()
        self._released_children: set[str] = set()

    def add(
        self, source: ImportSource, selector: str, value: FactValue, *, identity: str | None = None
    ) -> None:
        name = type(value).__name__
        fact = fact_value(value)
        fact.imported_from.append(source.provenance(selector))
        try:
            validate(fact)
        except ValueError as error:
            raise FactImportError(f"{source.path}:{selector}", str(error)) from error
        key = name.encode() + b"\0" + value.to_binary()
        if identity is not None:
            prior = self._identities.setdefault((name, identity), key)
            if prior != key:
                raise FactImportError(f"{source.path}:{selector}", f"conflicting {name} {identity}")
        if key in self._facts:
            origins = self._facts[key].imported_from
            if fact.imported_from[0] not in origins:
                origins.append(fact.imported_from[0])
        else:
            self._facts[key] = fact

    def finish(self, run_id: str) -> pb.ImportedRunFacts:
        facts = [
            self._facts[key] for key in sorted(self._facts) if not self._released(self._facts[key])
        ]
        facts = consolidate_guidance(facts)
        for fact in facts:
            fact.imported_from.sort(
                key=lambda origin: (origin.path, origin.selector, origin.sha256)
            )
        result = pb.ImportedRunFacts(schema_version=1, run_id=run_id, facts=facts)
        try:
            validate(result)
        except ValueError as error:
            raise FactImportError(run_id, str(error)) from error
        return result

    def project(self, source: ImportSource, selector: str, value: object) -> None:
        """Visit schema-validated fields only; strings are never scanned for plausible facts."""
        ensure_finite(source, selector, value)
        for path, item in walk(selector, value):
            self._project_value(source, path, item)

    def _project_value(self, source: ImportSource, path: str, item: object) -> None:
        if isinstance(item, RoundRecord):
            lower_round(source, path, item, self)
        elif isinstance(item, values.ArtifactRef):
            self.add(source, path, cast("core_common.ArtifactRef", to_proto(item)))
        elif isinstance(item, values.RevisionRef):
            self.add(
                source,
                path,
                pb.ImportedCandidate(revision=cast("core_leaves.RevisionRef", to_proto(item))),
            )
        elif isinstance(item, values.Sample):
            self.add(
                source,
                path,
                pb.ImportedMeasurement(sample=cast("core_leaves.Sample", to_proto(item))),
                identity=f"sample:{item.run.root}",
            )
        elif isinstance(item, EvidenceRef) and item.provenance == "trusted":
            self.add(
                source,
                path,
                pb.ImportedMeasurement(evidence=_evidence(item)),
                identity=f"evidence:{item.source_request.root}:{item.evidence_id.root}",
            )
        elif isinstance(item, ChargeReceipt):
            self.add(
                source,
                path,
                usage(
                    identity=item.charge_id.root,
                    unit=item.kind.value,
                    amount=item.charged,
                    refunded=item.refunded,
                    source_request=None
                    if item.source_request is None
                    else item.source_request.root,
                ),
                identity=item.charge_id.root,
            )
        elif isinstance(item, EvidenceEntry):
            self.add(
                source,
                path,
                stage_measurement(item),
                identity=f"stage:{item.evidence.evaluation_id}:{item.evidence.evidence_id}",
            )
        elif isinstance(item, InputRecord):
            self._guidance(source, path, item)
        elif isinstance(item, RunFacts):
            self.add(
                source,
                path,
                pb.ImportedInput(
                    baseline_revision=item.baseline.git_commit or item.baseline.revision_id.root,
                    baseline=cast("core_leaves.RevisionRef", to_proto(item.baseline)),
                    objective=item.objective,
                    evaluator_digest=item.evaluator_digest,
                    workload_digest=item.workload_digest,
                    environment_digest=item.environment_digest,
                    options=[
                        cast("core_contracts.PureOption", to_proto(option))
                        for option in item.options
                    ],
                    constraints=[
                        cast("core_contracts.PureOption", to_proto(option))
                        for option in item.constraints
                    ],
                    task=None if item.task is None else task(item.task),
                ),
            )
        else:
            self._ownership(source, path, item)

    def _ownership(self, source: ImportSource, path: str, item: object) -> None:
        if isinstance(item, OwnedJob):
            if item.released:
                self._released_resources.add(item.resource_id.root)
            else:
                self.external(
                    source, path, "evaluation", item.resource_id.root, item.submission_id.root
                )
        elif isinstance(item, RegisteredOwnedJob):
            self._registered_job(source, path, item)
        elif isinstance(item, ChildLease):
            self._child(source, path, item)
        elif isinstance(item, RequestRecord) and item.dispatched_in is not None:
            self._request(source, path, item)

    def _registered_job(self, source: ImportSource, path: str, item: RegisteredOwnedJob) -> None:
        if item.released:
            if item.resource_id is not None:
                self._released_resources.add(item.resource_id.root)
            return
        self.external(source, path, "registered-job", item.operation_id.root, item.request_id.root)
        if item.resource_id is not None:
            self.external(source, path, "resource", item.resource_id.root, item.request_id.root)
        for child in item.children:
            self.external(source, path, "child-resource", child.root, item.request_id.root)

    def _request(self, source: ImportSource, path: str, item: RequestRecord) -> None:
        observation = item.observation
        if observation is not None and observation.released:
            self._released_requests.add(item.request_id.root)
            return
        self.external(source, path, item.request.kind, item.request_id.root, item.request_id.root)
        if observation is not None:
            resources = (
                () if observation.resource_id is None else (observation.resource_id,)
            ) + observation.descendants
            for resource in resources:
                self.external(source, path, "resource", resource.root, item.request_id.root)

    def _released(self, fact: pb.ImportedFact) -> bool:
        if fact.value is None or fact.value.field != "external_identity":
            return False
        identity = fact.value.value
        if identity.kind == "child-resource":
            return identity.identity in self._released_children
        return identity.identity in self._released_resources or (
            identity.identity == identity.source_request
            and identity.source_request in self._released_requests
        )

    def _child(self, source: ImportSource, path: str, item: ChildLease) -> None:
        released = item.watermark_history_complete and all(
            mark.observation.released for mark in item.observation_watermarks
        )
        if released:
            self._released_children.add(item.resource_id.root)
        else:
            for request in item.source_requests:
                self.external(source, path, "child-resource", item.resource_id.root, request.root)

    def external(
        self, source: ImportSource, path: str, kind: str, identity: str, request: str | None = None
    ) -> None:
        self.add(
            source,
            path,
            pb.ImportedExternalIdentity(kind=kind, identity=identity, source_request=request),
        )

    def _guidance(self, source: ImportSource, path: str, item: InputRecord) -> None:
        self.add(
            source,
            path,
            pb.ImportedGuidance(
                identity=item.input.input_id.root,
                artifact=cast("core_common.ArtifactRef", to_proto(item.input.artifact)),
                delivered=isinstance(item.receipt, InputDelivered),
                withdrawn=isinstance(item.receipt, InputDropped)
                and item.receipt.reason.value == "withdrawn",
                disposed=isinstance(item.receipt, InputDropped),
                target=input_target(item.input.target),
                mode={
                    "next-turn": core_common.InputMode.NEXT_TURN,
                    "interrupt": core_common.InputMode.INTERRUPT,
                    "standing": core_common.InputMode.STANDING,
                }[item.input.mode.value],
                possibly_read=item.possibly_read,
            ),
        )


def walk(path: str, value: object) -> Iterator[tuple[str, object]]:
    """Traverse validated models without interpreting arbitrary dictionaries as contracts."""
    yield path, value
    if isinstance(value, BaseModel | values.CoreValue):
        for name in type(value).model_fields:
            yield from walk(f"{path}.{name}", getattr(value, name))
    elif is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            yield from walk(f"{path}.{field.name}", getattr(value, field.name))
    elif isinstance(value, tuple | list):
        for index, item in enumerate(value):
            yield from walk(f"{path}[{index}]", item)
    elif isinstance(value, dict):
        for key, item in sorted(value.items()):
            yield from walk(f"{path}.{key}", item)


def input_target(value: values.InputTarget) -> core_control.InputTarget:
    encoded = to_proto(value)
    match encoded:
        case core_control.ScopeInputTarget():
            result = core_control.InputTarget(target=Oneof("scope", encoded))
        case core_control.ItemInputTarget():
            result = core_control.InputTarget(target=Oneof("item", encoded))
        case core_control.InvocationInputTarget():
            result = core_control.InputTarget(target=Oneof("invocation", encoded))
        case core_control.RoleInputTarget():
            result = core_control.InputTarget(target=Oneof("role", encoded))
        case _:
            message = "unknown guidance target"
            raise TypeError(message)
    return result


def usage(
    identity: str,
    unit: str,
    amount: int | float | None,
    *,
    source_request: str | None = None,
    refunded: int = 0,
) -> pb.ImportedUsage:
    if amount is None:
        quantity = Oneof("unknown", value=True)
    elif isinstance(amount, int):
        quantity = Oneof("count", str(amount))
    else:
        quantity = Oneof("amount", amount)
    return pb.ImportedUsage(
        identity=identity,
        unit=unit,
        quantity=quantity,
        source_request=source_request,
        refunded_count=str(refunded),
    )


def task(value: TaskTerms) -> pb.ImportedTask:
    return pb.ImportedTask(
        recipe=cast("core_common.ArtifactRef", to_proto(value.recipe)),
        stages=[cast("core_leaves.TaskStage", to_proto(stage)) for stage in value.stages],
        measured=list(value.measured),
        queue_allowance=value.queue_allowance,
        accuracy_stage=value.accuracy_stage,
        benchmark_stage=value.benchmark_stage,
        accuracy_gated=[to_proto_kind(kind) for kind in value.accuracy_gated],
        operation_seconds=value.operation_seconds,
        objectives=[
            cast("core_leaves.TaskObjective", to_proto(objective)) for objective in value.objectives
        ],
        relative_noise=value.relative_noise,
        base_staleness=value.base_staleness,
        runaway_evaluations=str(value.runaway_evaluations),
        profile_fields=list(value.profile_fields),
    )


def to_proto_kind(kind: values.EvidenceKind) -> core_vocabulary.EvidenceKind:
    return {
        values.EvidenceKind.CORRECTNESS: core_vocabulary.EvidenceKind.CORRECTNESS,
        values.EvidenceKind.BENCHMARK: core_vocabulary.EvidenceKind.BENCHMARK,
        values.EvidenceKind.PROFILING: core_vocabulary.EvidenceKind.PROFILING,
    }[kind]


def content(data: bytes, media_type: str) -> pb.ImportedContent:
    return pb.ImportedContent(sha256=sha256(data).hexdigest(), data=data, media_type=media_type)


def _evidence(value: EvidenceRef) -> core_agent_facts.EvidenceRef:
    receipt = value.acceptance_receipt
    acceptance = (
        None
        if receipt is None
        else values.EvidenceAcceptanceReceipt.model_validate(
            {
                "source_request": value.source_request,
                "scope": value.scope,
                "observation_sequence": value.observation_sequence,
                "status": value.status,
            }
        )
    )
    shared = values.EvidenceRef(
        evidence_id=value.evidence_id,
        kind=value.kind,
        purpose=value.purpose,
        scope=value.scope,
        source_request=value.source_request,
        candidate=value.candidate,
        observation_sequence=value.observation_sequence,
        evaluator_digest=value.evaluator_digest,
        workload_digest=value.workload_digest,
        environment_digest=value.environment_digest,
        provenance=value.provenance,
        status=value.status,
        artifacts=value.artifacts,
        acceptance_receipt=acceptance,
        reused_from=value.reused_from,
        reading=value.reading,
        unmet_requirement=value.unmet_requirement,
    )
    return cast("core_agent_facts.EvidenceRef", to_proto(shared))


type FactValue = (
    pb.ImportedInput
    | pb.ImportedCandidate
    | pb.ImportedMeasurement
    | pb.ImportedGuidance
    | pb.ImportedUsage
    | pb.ImportedExternalIdentity
    | pb.ImportedContent
    | core_common.ArtifactRef
)


def fact_value(value: FactValue) -> pb.ImportedFact:
    match value:
        case pb.ImportedInput():
            result = pb.ImportedFact(value=Oneof("input", value))
        case pb.ImportedCandidate():
            result = pb.ImportedFact(value=Oneof("candidate", value))
        case pb.ImportedMeasurement():
            result = pb.ImportedFact(value=Oneof("measurement", value))
        case pb.ImportedGuidance():
            result = pb.ImportedFact(value=Oneof("guidance", value))
        case pb.ImportedUsage():
            result = pb.ImportedFact(value=Oneof("usage", value))
        case pb.ImportedExternalIdentity():
            result = pb.ImportedFact(value=Oneof("external_identity", value))
        case core_common.ArtifactRef():
            result = pb.ImportedFact(value=Oneof("content_reference", value))
        case pb.ImportedContent():
            result = pb.ImportedFact(value=Oneof("content", value))
    return result
