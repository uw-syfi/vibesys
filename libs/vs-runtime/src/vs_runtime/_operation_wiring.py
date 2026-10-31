"""Production wiring of the operation catalog and the executor bindings behind it.

A strategy declares its operations (codec registrations) and says which role each
plays. The runtime owns what performs each role. This module joins the two:
``build_operation_catalog`` makes the closed catalog, giving every role an owner or
a typed refusal, and ``bind_operations`` is the startup gate. It rejects a strategy
whose required operation has no owner, then returns the shell bindings that execute
the catalog. Tests build their catalogs through the same functions.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from vs_core.api import (
    CAPTURE_SUFFIX,
    EXPORT_PATCH_SUFFIX,
    INTERPRET_SUFFIX,
    RENDER_SUFFIX,
    RETAIN_SUFFIX,
    VALIDATE_LOCAL_SUFFIX,
    VERIFY_SUFFIX,
    ContractError,
    OperationRegistry,
)
from vs_runtime._capture_command_operation import CaptureCommandOwner
from vs_runtime._core_loop import CoreRuntimeBindings
from vs_runtime._evidence_operations import InterpretEvidenceOwner, RetainRevisionOwner
from vs_runtime._export_patch_operation import ExportPatchOwner
from vs_runtime._local_validation import LocalValidator
from vs_runtime._observation_factory import ObservationFactory
from vs_runtime._operation_catalog import OperationCatalog, OperationEntry, RefusalReason
from vs_runtime._operation_receipts import NamespaceOperationReceipts
from vs_runtime._operation_requests import RegisteredOperationRequests
from vs_runtime._receipt_store import ReceiptStore
from vs_runtime._render_operation import RenderArtifactsOwner
from vs_runtime._rerun_policy import (
    ClearedWork,
    KeyedWrite,
    ReadOnly,
    RerunPolicy,
    UnwiredWorkCleaner,
    WorkCleaner,
)
from vs_runtime._validate_local_operation import ValidateLocalOwner
from vs_runtime._verify_revision_operation import VerifyRevisionOwner

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from pydantic import BaseModel

    from vs_core.api import (
        OperationRegistration,
        RevisionRef,
        SchemaRef,
        SearchDeclaration,
        StrategyOperations,
    )
    from vs_project.api import StateNamespace
    from vs_prompts.api import TemplateRenderer
    from vs_runtime._artifact_store import ArtifactStore
    from vs_runtime._core_requests import RequestExecutors
    from vs_runtime._evidence_ledger import EvidenceLookup
    from vs_runtime._local_validation import LocalValidationEvents
    from vs_runtime._operation_catalog import OperationOwner
    from vs_runtime._validation_passes import ValidationPasses
    from vs_runtime.contracts import CaptureCommands, Commands, RevisionLedger, Workspaces
    from vs_sim.api import Threads


class OperationRole(StrEnum):
    """What the runtime performs for a strategy, independent of the strategy's own kind names."""

    RENDER_ARTIFACTS = "render_artifacts"
    VERIFY_REVISION = "verify_revision"
    INTERPRET_EVIDENCE = "interpret_evidence"
    RETAIN_REVISION = "retain_revision"
    CAPTURE_COMMAND = "capture_command"
    VALIDATE_LOCAL = "validate_local"
    EXPORT_PATCH = "export_patch"


# A role whose owner is absent is declared but refused with the contract it lacks.
# A role not listed here cannot be refused: wiring must supply its owner.
_REFUSALS: Mapping[OperationRole, tuple[RefusalReason, str]] = {
    OperationRole.INTERPRET_EVIDENCE: (
        RefusalReason.NO_EVIDENCE_LOOKUP,
        "no evidence lookup was wired to read recorded evaluation evidence",
    ),
    OperationRole.RETAIN_REVISION: (
        RefusalReason.NO_ACCURACY_PROOF,
        "no workspace was wired to retain a revision its accuracy proof names",
    ),
    OperationRole.CAPTURE_COMMAND: (
        RefusalReason.NO_COMMAND_RUNNER,
        "no command runner was wired to execute a captured command in a workspace",
    ),
    OperationRole.VALIDATE_LOCAL: (
        RefusalReason.NO_COMMAND_RUNNER,
        "no command runner and isolated candidate workspaces were wired to run validation recipes",
    ),
}


def operation_roles(operations: StrategyOperations) -> dict[OperationRole, OperationRegistration]:
    """The registration of each runtime role, from a strategy's four operations.

    The runtime performs an operation by role, independent of the strategy's kind names, so
    a strategy that declares its operations through `StrategyOperations` gets its owners.
    """
    return {
        role: registration
        for role, suffix in _SUFFIX_OF_ROLE.items()
        if (registration := operations.registration_of(suffix)) is not None
    }


# The suffix of the operation kind each role is declared under (`StrategyOperations`).
_SUFFIX_OF_ROLE = {
    OperationRole.RENDER_ARTIFACTS: RENDER_SUFFIX,
    OperationRole.VERIFY_REVISION: VERIFY_SUFFIX,
    OperationRole.INTERPRET_EVIDENCE: INTERPRET_SUFFIX,
    OperationRole.RETAIN_REVISION: RETAIN_SUFFIX,
    OperationRole.CAPTURE_COMMAND: CAPTURE_SUFFIX,
    OperationRole.VALIDATE_LOCAL: VALIDATE_LOCAL_SUFFIX,
    OperationRole.EXPORT_PATCH: EXPORT_PATCH_SUFFIX,
}


@dataclass(frozen=True)
class OperationPorts:
    """What the runtime owners need from the run: workspaces, artifacts, evidence and encodings."""

    renderer: TemplateRenderer
    artifacts: ArtifactStore
    workspaces: Workspaces
    ledger: RevisionLedger
    evidence: EvidenceLookup
    commit_of: Callable[[RevisionRef], str | None]
    retention_label: str
    validation_passes: ValidationPasses
    variables: Mapping[str, object] = field(default_factory=dict)
    commands: Commands | None = None
    capture_commands: CaptureCommands | None = None
    replies: Mapping[SchemaRef, type[BaseModel]] = field(default_factory=dict)
    local_validation_events: LocalValidationEvents | None = None


def _command_owner(role: OperationRole, ports: OperationPorts) -> OperationOwner | None:
    """The owner of a role that runs commands, or None when the run cannot offer it."""
    if role is OperationRole.CAPTURE_COMMAND:
        if ports.capture_commands is None:
            return None
        return CaptureCommandOwner(
            ports.workspaces, ports.capture_commands, ports.ledger, ports.commit_of
        )
    if ports.commands is None:
        return None
    if not ports.workspaces.supports_parallel_candidates:
        return None
    return ValidateLocalOwner(
        ports.workspaces,
        LocalValidator(ports.commands, ports.validation_passes),
        ports.ledger,
        ports.commit_of,
        ports.artifacts,
        events=ports.local_validation_events,
    )


def production_owners(
    roles: Iterable[OperationRole], ports: OperationPorts
) -> Mapping[OperationRole, OperationOwner]:
    """The owner of each requested role, so a strategy that registers a role gets its owner."""
    owners: dict[OperationRole, OperationOwner] = {}
    for role in roles:
        match role:
            case OperationRole.RENDER_ARTIFACTS:
                owners[role] = RenderArtifactsOwner(
                    ports.renderer, ports.artifacts, ports.variables, ports.evidence, ports.replies
                )
            case OperationRole.VERIFY_REVISION:
                owners[role] = VerifyRevisionOwner(ports.workspaces, ports.ledger, ports.commit_of)
            case OperationRole.INTERPRET_EVIDENCE:
                owners[role] = InterpretEvidenceOwner(ports.evidence)
            case OperationRole.RETAIN_REVISION:
                owners[role] = RetainRevisionOwner(
                    ports.workspaces, ports.ledger, ports.commit_of, ports.retention_label
                )
            case OperationRole.EXPORT_PATCH:
                owners[role] = ExportPatchOwner(ports.workspaces, ports.ledger, ports.commit_of)
            case OperationRole.CAPTURE_COMMAND | OperationRole.VALIDATE_LOCAL:
                if (owner := _command_owner(role, ports)) is not None:
                    owners[role] = owner
    return owners


# What a rerun of each role's operation may repeat: the role fixes it, not the owner instance.
# The two command roles run a process, so their policy is a cleaner the run supplies.
_RERUN_OF_ROLE: Mapping[OperationRole, RerunPolicy] = {
    OperationRole.RENDER_ARTIFACTS: KeyedWrite("the digest of the rendered artifact"),
    OperationRole.VERIFY_REVISION: ReadOnly(),
    OperationRole.INTERPRET_EVIDENCE: ReadOnly(),
    OperationRole.RETAIN_REVISION: KeyedWrite("the retention label of the request"),
    OperationRole.EXPORT_PATCH: ReadOnly(),
}


def _rerun_of(role: OperationRole, cleaner: WorkCleaner) -> RerunPolicy:
    return _RERUN_OF_ROLE.get(role) or ClearedWork(cleaner)


def build_operation_catalog(
    registrations: Mapping[OperationRole, OperationRegistration],
    owners: Mapping[OperationRole, OperationOwner],
    *,
    extra: tuple[OperationEntry, ...] = (),
    cleaner: WorkCleaner | None = None,
) -> OperationCatalog:
    """The closed catalog for the roles a strategy registered.

    Each registered role has its owner when ``owners`` supplies one, else a typed
    refusal. Only roles with a declared refusal may lack an owner, and an owner for
    an unregistered role is an error, so wiring drift fails here and not mid-run.
    ``extra`` carries entries for operations outside these roles. *cleaner* ends the
    work a command role's interrupted attempt left; a run that wires none cannot clear
    one, which the step loop reports instead of repeating it.
    """
    work = cleaner or UnwiredWorkCleaner()
    stray = sorted(role for role in owners if role not in registrations)
    if stray:
        raise ContractError(("owners", stray[0]), "owner supplied for a role with no registration")
    entries: list[OperationEntry] = []
    for role, registration in registrations.items():
        owner = owners.get(role)
        if owner is not None:
            entries.append(OperationEntry.owned(registration, owner, _rerun_of(role, work)))
        elif role in _REFUSALS:
            reason, detail = _REFUSALS[role]
            entries.append(OperationEntry.refused(registration, reason, detail))
        else:
            raise ContractError(("owners", role), "role has no owner and cannot be refused")
    all_entries = (*entries, *extra)
    registry = OperationRegistry(tuple(entry.registration for entry in all_entries))
    return OperationCatalog(registry, all_entries)


def bind_operations(
    declaration: SearchDeclaration,
    catalog: OperationCatalog,
    receipts: StateNamespace,
    *,
    threads: Threads,
    base: CoreRuntimeBindings | None = None,
) -> CoreRuntimeBindings:
    """Startup gate and shell bindings: reject unowned required operations, then execute the catalog.

    Raises ``ContractError`` naming the first required operation that is missing
    or refused. ``base`` supplies the other executor roles unchanged; ``threads``
    locks the receipt store.
    """
    catalog.require_owned(declaration)
    selected = base or CoreRuntimeBindings()
    store = ReceiptStore(receipts, threads)
    operations = RegisteredOperationRequests(
        catalog, NamespaceOperationReceipts(store), ObservationFactory(store)
    )
    executors: RequestExecutors = dataclasses.replace(selected.executors, operations=operations)
    return dataclasses.replace(selected, registry=catalog.registry, executors=executors)
