"""Thin read-only Project shell for the one pure facts importer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._manifests import read_manifest
from ._source import MAX_SOURCE_BYTES
from .api import FactImportError, ImportSource, SourceFormat, import_run_facts

if TYPE_CHECKING:
    from vs_core.api import OperationRegistry
    from vs_project.api import Project, StateFile
    from vs_proto.api import imported_facts as pb

__all__ = ["read_run_facts"]

_FORMATS = {
    "run.json": SourceFormat.MANIFEST,
    "publications/publications.json": SourceFormat.PUBLICATIONS,
    "dynamic/state.json": SourceFormat.LEGACY_DYNAMIC,
    "single/state.json": SourceFormat.HYPOTHESES,
    "multi/state.json": SourceFormat.HYPOTHESES,
    "agent/state.json": SourceFormat.HYPOTHESES,
    "core-store/store.json": SourceFormat.PROJECT_STORE,
}
_CONTENT_NAMESPACES = frozenset({"artifacts", "runtime", "progress", "handoffs", "evidence"})


def _portable(document: StateFile) -> ImportSource | None:
    path = document.relative_path.as_posix()
    if path in _FORMATS:
        return ImportSource(path, _FORMATS[path], document.contents)
    if document.relative_path.name == "state.json":
        raise FactImportError(path, "unknown retained state format")
    if document.relative_path.parts[0] in _CONTENT_NAMESPACES:
        return ImportSource(path, SourceFormat.CONTENT, document.contents)
    return None


def read_run_facts(
    project: Project, run_id: str, *, registry: OperationRegistry
) -> pb.ImportedRunFacts:
    """Read a stopped run through Project and import it without changing a single file.

    This function grants no exclusion or dispatch authority. Its caller must stop old
    writers before taking this source set and keep exclusive ownership until publishing
    the new epoch. It reads original retained files, never the user's edited task root.
    """
    documents = project.state.retained_run_documents(run_id)
    sources = [source for document in documents if (source := _portable(document)) is not None]
    manifest = next((source for source in sources if source.format == SourceFormat.MANIFEST), None)
    if manifest is None:
        raise FactImportError("run.json", "original run manifest is required")
    strategy, _, _ = read_manifest(manifest, registry)
    for document in project.state.retained_run_documents(run_id, local_namespace="receipts"):
        path = document.relative_path.as_posix()
        if path == "receipts.lock":
            continue
        sources.append(ImportSource(f"receipts/{path}", SourceFormat.RECEIPT, document.contents))
    sources.extend(
        ImportSource("agent/sessions.json", SourceFormat.SESSIONS, document.contents)
        for document in project.state.retained_run_documents(run_id, local_namespace="agent")
        if document.relative_path.as_posix() == "sessions.json"
    )
    controls = project.state.retained_run_documents(run_id, local_namespace="control")
    settled = {
        document.relative_path.name
        for document in controls
        if document.relative_path.parts[0] in ("settled", "rejected")
    }
    sources.extend(
        ImportSource(f"control/{document.relative_path}", SourceFormat.CONTROL, document.contents)
        for document in controls
        if document.relative_path.parts[0] == "inbox" and document.relative_path.name not in settled
    )
    data = project.retained_event_journal(run_id, max_bytes=MAX_SOURCE_BYTES)
    if data is not None:
        sources.append(ImportSource("core-events.jsonl", SourceFormat.EVENTS, data))
    return import_run_facts(run_id, sources, strategy=strategy, registry=registry)
