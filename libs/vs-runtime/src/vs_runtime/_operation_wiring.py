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

from vs_core.api import ContractError, OperationRegistry
from vs_runtime._core_loop import CoreRuntimeBindings
from vs_runtime._evidence_operations import InterpretEvidenceOwner, RetainRevisionOwner
from vs_runtime._observation_factory import ObservationFactory
from vs_runtime._operation_catalog import OperationCatalog, OperationEntry, RefusalReason
from vs_runtime._operation_receipts import NamespaceOperationReceipts
from vs_runtime._operation_requests import RegisteredOperationRequests
from vs_runtime._receipt_store import ReceiptStore
from vs_runtime._render_operation import RenderArtifactsOwner
from vs_runtime._verify_revision_operation import VerifyRevisionOwner

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from vs_core.api import OperationRegistration, RevisionRef, StrategyDeclaration
    from vs_project.api import StateNamespace
    from vs_prompts.api import TemplateRenderer
    from vs_runtime._artifact_store import ArtifactStore
    from vs_runtime._core_requests import RequestExecutors
    from vs_runtime._evidence_ledger import EvidenceLookup
    from vs_runtime._operation_catalog import OperationOwner
    from vs_runtime.contracts import RevisionLedger, Workspaces


class OperationRole(StrEnum):
    """What the runtime performs for a strategy, independent of the strategy's own kind names."""

    RENDER_ARTIFACTS = "render_artifacts"
    VERIFY_REVISION = "verify_revision"
    INTERPRET_EVIDENCE = "interpret_evidence"
    RETAIN_REVISION = "retain_revision"


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
    variables: Mapping[str, object] = field(default_factory=dict)


def production_owners(
    roles: Iterable[OperationRole], ports: OperationPorts
) -> Mapping[OperationRole, OperationOwner]:
    """The owner of each requested role, so a strategy that registers a role gets its owner."""
    owners: dict[OperationRole, OperationOwner] = {}
    for role in roles:
        match role:
            case OperationRole.RENDER_ARTIFACTS:
                owners[role] = RenderArtifactsOwner(
                    ports.renderer, ports.artifacts, ports.variables
                )
            case OperationRole.VERIFY_REVISION:
                owners[role] = VerifyRevisionOwner(ports.workspaces, ports.ledger, ports.commit_of)
            case OperationRole.INTERPRET_EVIDENCE:
                owners[role] = InterpretEvidenceOwner(ports.evidence)
            case OperationRole.RETAIN_REVISION:
                owners[role] = RetainRevisionOwner(
                    ports.workspaces, ports.ledger, ports.commit_of, ports.retention_label
                )
    return owners


def build_operation_catalog(
    registrations: Mapping[OperationRole, OperationRegistration],
    owners: Mapping[OperationRole, OperationOwner],
    *,
    extra: tuple[OperationEntry, ...] = (),
) -> OperationCatalog:
    """The closed catalog for the roles a strategy registered.

    Each registered role has its owner when ``owners`` supplies one, else a typed
    refusal. Only roles with a declared refusal may lack an owner, and an owner for
    an unregistered role is an error, so wiring drift fails here and not mid-run.
    ``extra`` carries entries for operations outside these roles.
    """
    stray = sorted(role for role in owners if role not in registrations)
    if stray:
        raise ContractError(("owners", stray[0]), "owner supplied for a role with no registration")
    entries: list[OperationEntry] = []
    for role, registration in registrations.items():
        owner = owners.get(role)
        if owner is not None:
            entries.append(OperationEntry.owned(registration, owner))
        elif role in _REFUSALS:
            reason, detail = _REFUSALS[role]
            entries.append(OperationEntry.refused(registration, reason, detail))
        else:
            raise ContractError(("owners", role), "role has no owner and cannot be refused")
    all_entries = (*entries, *extra)
    registry = OperationRegistry(tuple(entry.registration for entry in all_entries))
    return OperationCatalog(registry, all_entries)


def bind_operations(
    declaration: StrategyDeclaration,
    catalog: OperationCatalog,
    receipts: StateNamespace,
    *,
    base: CoreRuntimeBindings | None = None,
) -> CoreRuntimeBindings:
    """Startup gate and shell bindings: reject unowned required operations, then execute the catalog.

    Raises ``ContractError`` naming the first required operation that is missing
    or refused. ``base`` supplies the other executor roles unchanged.
    """
    catalog.require_owned(declaration)
    selected = base or CoreRuntimeBindings()
    store = ReceiptStore(receipts)
    operations = RegisteredOperationRequests(
        catalog, NamespaceOperationReceipts(store), ObservationFactory(store)
    )
    executors: RequestExecutors = dataclasses.replace(selected.executors, operations=operations)
    return dataclasses.replace(selected, registry=catalog.registry, executors=executors)
