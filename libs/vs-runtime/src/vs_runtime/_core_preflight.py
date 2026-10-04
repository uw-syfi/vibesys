"""Read-only fresh-only resume resolution, before opening any execution resources."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict

from vs_core.api import ContractError, OperationRegistry, Strategy, StrategyState, validate_startup
from vs_project.api import Project, StateStore, StoredEnvelope
from vs_runtime._core_record import RuntimeRecord

if TYPE_CHECKING:
    from vs_project.api import StateNamespace


class ResumeDiagnostic(BaseModel):
    """Stable transport-neutral preflight failure with source path and schema."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    code: Literal["dynamic_legacy_resume_unsupported", "core_resume_invalid"]
    stage: Literal["resume_resolution"] = "resume_resolution"
    path: str
    source_schema: str
    message: str


class CoreResumeError(ValueError):
    """No backend, evaluator, workspace or agent may open after this failure."""

    def __init__(self, diagnostic: ResumeDiagnostic) -> None:
        self.diagnostic = diagnostic
        super().__init__(diagnostic.message)


@dataclass(frozen=True)
class ResolvedCoreResume[S: StrategyState]:
    """Validated selected run and store; no execution resource has been created."""

    run_id: str
    store: StateStore
    record: RuntimeRecord[S]


def _schema(source: bytes) -> str:
    try:
        value = json.loads(source)
    except (ValueError, UnicodeDecodeError):
        return "unparseable"
    if isinstance(value, dict):
        version = value.get("schema_version")
        if isinstance(version, int) and not isinstance(version, bool):
            return str(version)
    return "unversioned"


def _path(namespace: StateNamespace, filename: str) -> str:
    return namespace.agent_visible_path(filename)


def _legacy(path: str, schema: str) -> CoreResumeError:
    return CoreResumeError(
        ResumeDiagnostic(
            code="dynamic_legacy_resume_unsupported",
            path=path,
            source_schema=schema,
            message=f"legacy dynamic resume unsupported: {path} (schema {schema}); a validated core envelope is required",
        )
    )


def _missing_envelope(project: Project, run_id: str) -> CoreResumeError:
    for item in project.state.portable_run_export(run_id).files:
        if (
            item.relative_path.suffix == ".jsonl"
            or "journal" in item.relative_path.parts
            or "agent" in item.relative_path.parts
        ):
            parts = item.relative_path.parts
            namespace = project.state.portable_namespace(run_id, parts[0])
            return _legacy(_path(namespace, "/".join(parts[1:])), _schema(item.contents))
    namespace = project.state.state_store_namespace(run_id)
    return _legacy(_path(namespace, "store.json"), "absent")


def resolve_core_resume[S: StrategyState](
    project: Project,
    strategy: Strategy[S],
    *,
    run_id: str | None = None,
    registry: OperationRegistry | None = None,
) -> ResolvedCoreResume[S]:
    """Resolve via Project and reject legacy evidence without modifying files.

    A legacy dynamic state always rejects. Journals alone never manufacture
    request or ownership authority. A new record must match the selected run,
    strategy schema/declaration and durable/offered operation catalog. Call this
    before selecting or opening any concrete execution implementation.
    """
    selected = project.state.resolve_run(run_id)
    legacy_namespace = project.state.portable_namespace(selected.run_id, "dynamic")
    legacy = legacy_namespace.read_bytes("state.json")
    if legacy is not None:
        raise _legacy(_path(legacy_namespace, "state.json"), _schema(legacy))
    store_namespace = project.state.state_store_namespace(selected.run_id)
    # StateStore.load synchronizes an existing record under its lock. Probe
    # absence through Project first so rejecting journal-only legacy runs does
    # not materialize a new core-store lock or directory.
    if store_namespace.read_bytes("store.json") is None:
        stored = None
    else:
        stored = project.state_store(selected.run_id).load()
    if stored is None:
        raise _missing_envelope(project, selected.run_id)
    registry = registry or OperationRegistry()
    record_model = cast(
        "type[RuntimeRecord[S]]", RuntimeRecord.__class_getitem__(type(strategy.state))
    )
    try:
        if not isinstance(stored, StoredEnvelope):
            raise ContractError(("runtime",), "quarantined record cannot resume")
        record = record_model.decode(stored, registry)
        envelope = record.envelope
        if envelope.core.run.run_id.root != selected.run_id:
            raise ContractError(("run_id",), "core envelope differs from selected run")
        if envelope.core.run.declaration != strategy.declaration:
            raise ContractError(
                ("declaration",), "offered strategy differs from durable declaration"
            )
        capabilities = validate_startup(strategy.declaration, envelope.core.run.capabilities)
        if capabilities.operations != envelope.core.registry:
            raise ContractError(("registry",), "offered operations differ from durable registry")
    except ValueError as error:
        namespace = project.state.state_store_namespace(selected.run_id)
        path = _path(namespace, "store.json")
        schema = str(stored.schema_version)
        raise CoreResumeError(
            ResumeDiagnostic(
                code="core_resume_invalid",
                path=path,
                source_schema=schema,
                message=f"invalid core resume: {path} (runtime schema {schema}): {error}",
            )
        ) from error
    return ResolvedCoreResume(selected.run_id, project.state_store(selected.run_id), record)
