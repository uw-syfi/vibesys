"""One permanent, pure facts-only importer of retained run storage.

``import_run_facts`` strictly decodes every supplied document and emits a validated
shared protobuf. It does not execute a flow, migrate snapshots, modify storage or
infer scheduling continuity. Corruption and unknown formats raise FactImportError
with the source path. The Project reader is separately exposed by ``read_run_facts``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import JsonValue, TypeAdapter

from vs_agent.api import AgentSessionState
from vs_core.api import FlowState, OperationRegistry, OperationResult, RunEnvelope
from vs_project.api import OrchestrationRunManifest, StoredEnvelope
from vs_project.api.retained import decode_store
from vs_proto.api import imported_facts as pb
from vs_runtime.api.core import PublicationHistory, RuntimeRecord
from vs_runtime.api.retained import decode_receipt

from ._controls import import_control
from ._events import import_events
from ._facts import FactSet, content
from ._rounds import import_round
from ._snapshots import SnapshotDecoder
from ._source import MAX_SOURCE_BYTES, FactImportError, ImportSource, SourceFormat, decode

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["FactImportError", "ImportSource", "SourceFormat", "import_run_facts"]


class _Importer:
    def __init__(self, registry: OperationRegistry, strategy: str, run_id: str) -> None:
        self.registry = registry
        self.run_id = run_id
        self.strategy = strategy
        self.facts = FactSet()
        self.snapshots = SnapshotDecoder(registry, self.facts, strategy)

    def ingest(self, source: ImportSource) -> None:
        reader = {
            SourceFormat.PROJECT_STORE: self.store,
            SourceFormat.RECEIPT: self.receipt,
            SourceFormat.MANIFEST: self.manifest,
            SourceFormat.PUBLICATIONS: self.publications,
            SourceFormat.ROUND: self.round,
            SourceFormat.CONTENT: self.content,
            SourceFormat.EVENTS: self.events,
            SourceFormat.CONTROL: self.control,
            SourceFormat.SESSIONS: self.sessions,
        }.get(source.format)
        if reader is not None:
            reader(source)
        else:
            self.state(source)

    def events(self, source: ImportSource) -> None:
        import_events(source, self.registry, self.facts, self.run_id)

    def control(self, source: ImportSource) -> None:
        import_control(source, self.registry, self.facts)

    def sessions(self, source: ImportSource) -> None:
        state = decode(source, TypeAdapter(AgentSessionState), self.registry)
        for name, session in sorted(state.sessions.items()):
            self.facts.external(
                source, f"$.sessions.{name}", "provider-session", session.session_id
            )

    def content(self, source: ImportSource) -> None:
        self.facts.add(source, "$", content(source.data, "application/octet-stream"))

    def round(self, source: ImportSource) -> None:
        import_round(source, self.registry, self.facts)

    def state(self, source: ImportSource) -> None:
        match source.format:
            case SourceFormat.RUNTIME:
                record = decode(source, TypeAdapter(RuntimeRecord[FlowState]), self.registry)
                self.envelope(source, record.envelope, "$.envelope")
                self.facts.project(source, "$", record)
                self.flow(source, "$.envelope.strategy", record.envelope.strategy)
            case SourceFormat.ENVELOPE:
                envelope = decode(source, TypeAdapter(RunEnvelope[FlowState]), self.registry)
                self.envelope(source, envelope, "$")
                self.facts.project(source, "$", envelope)
                self.flow(source, "$.strategy", envelope.strategy)
            case SourceFormat.FLOW:
                state = decode(source, TypeAdapter(FlowState), self.registry)
                self.facts.project(source, "$", state)
                self.flow(source, "$", state)
            case SourceFormat.LEGACY_DYNAMIC:
                self.snapshots.read(source, "$", source.data, "legacy")
            case SourceFormat.HYPOTHESES:
                self.snapshots.read(source, "$", source.data, "hypotheses")
            case _:
                raise FactImportError(source.path, f"unsupported retained format {source.format}")

    def publications(self, source: ImportSource) -> None:
        history = decode(source, TypeAdapter(PublicationHistory), self.registry)
        self.facts.project(source, "$", history)

    def store(self, source: ImportSource) -> None:
        try:
            stored = decode_store(source.path, source.data)
        except ValueError as error:
            raise FactImportError(source.path, str(error)) from error
        if stored is None:
            return
        if stored.schema_version != 1:
            raise FactImportError(
                f"{source.path}:$.record.schema_version", "unknown runtime record version"
            )
        if not isinstance(stored, StoredEnvelope):
            raise FactImportError(source.path, f"quarantined source: {stored.reason}")
        record = decode(
            source,
            TypeAdapter(RuntimeRecord[FlowState]),
            self.registry,
            selector="$.record.payload",
            data=stored.payload,
        )
        self.envelope(source, record.envelope, "$.record.payload.envelope")
        self.facts.project(source, "$.record.payload", record)
        self.flow(source, "$.record.payload.envelope.strategy", record.envelope.strategy)

    def receipt(self, source: ImportSource) -> None:
        path = source.path.removeprefix("receipts/")
        try:
            receipt = decode_receipt(path, source.data, self.registry)
        except ValueError as error:
            raise FactImportError(source.path, str(error)) from error
        for index, value in enumerate(receipt.values):
            self.facts.project(source, f"$[{index}]", value)
        for identity in receipt.external_identities:
            self.facts.external(
                source, "$", identity.kind, identity.identity, identity.source_request
            )

    def envelope(self, source: ImportSource, envelope: RunEnvelope[FlowState], path: str) -> None:
        if envelope.schema_version not in range(34, 43):
            raise FactImportError(
                f"{source.path}:{path}.schema_version", "unknown retained envelope version"
            )
        if envelope.core.run.run_id.root != self.run_id:
            raise FactImportError(
                f"{source.path}:{path}.core.run.run_id", "source belongs to another run"
            )

    def manifest(self, source: ImportSource) -> None:
        manifest = decode(source, TypeAdapter(OrchestrationRunManifest), self.registry)
        if manifest.run_id != self.run_id:
            raise FactImportError(f"{source.path}:$.run_id", "source belongs to another run")
        if manifest.orchestration.id != self.strategy:
            raise FactImportError(f"{source.path}:$.orchestration.id", "strategy mismatch")
        if manifest.orchestration.config_version != 1:
            raise FactImportError(
                f"{source.path}:$.orchestration.config_version", "unknown retained config version"
            )
        self.snapshots.read(
            source,
            "$.orchestration.options",
            TypeAdapter(dict[str, JsonValue]).dump_json(manifest.orchestration.options).decode(),
            "launch",
        )
        self.facts.add(
            source,
            "$",
            pb.ImportedInput(
                baseline_revision=manifest.trusted_input_baseline,
                input_fingerprint=manifest.input_fingerprint,
                config=[content(source.data, "application/vnd.vibesys.legacy-manifest+json")],
            ),
        )

    def flow(self, source: ImportSource, path: str, state: FlowState) -> None:
        if state.schema_version != 1:
            raise FactImportError(
                f"{source.path}:{path}.schema_version", "unknown retained FlowState version"
            )
        for section_name, section in (("live", state.live), ("base", state.base)):
            if section is None:
                continue
            for index, flow in enumerate(section.flows):
                selector = f"{path}.{section_name}.flows[{index}].snapshot_json"
                self.snapshots.read(source, selector, flow.snapshot_json, flow.name)
            if section.published is not None:
                selector = f"{path}.{section_name}.published"
                self.snapshots.read(source, selector, section.published, "published")
        for index, step in enumerate(state.journal):
            if step.recorded is not None:
                selector = f"{path}.journal[{index}].recorded.result_json"
                result = decode(
                    source,
                    TypeAdapter(OperationResult),
                    self.registry,
                    selector=selector,
                    data=step.recorded.result_json,
                )
                self.facts.project(source, selector, result)


def import_run_facts(
    run_id: str,
    sources: Iterable[ImportSource],
    *,
    strategy: str,
    registry: OperationRegistry,
) -> pb.ImportedRunFacts:
    """Import a quiescent source set; input order does not change output bytes.

    The caller must stop old writers and retain originals. This pure function grants
    no writer authority and does not convert storage. Missing facts remain missing;
    source absence never supplies synthetic trust, zero cost or a released resource.
    """
    importer = _Importer(registry, strategy, run_id)
    documents = sorted(sources, key=lambda source: (source.path, source.format))
    paths: set[str] = set()
    for source in documents:
        if not source.path or source.path in paths:
            raise FactImportError(source.path or "<source>", "empty or duplicate source path")
        paths.add(source.path)
        if len(source.data) > MAX_SOURCE_BYTES:
            raise FactImportError(source.path, "retained document exceeds byte limit")
        importer.ingest(source)
    if not documents:
        raise FactImportError(run_id, "no retained content; original input is required")
    return importer.facts.finish(run_id)
