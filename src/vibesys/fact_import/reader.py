"""Thin read-only Project shell for the one pure facts importer."""

from __future__ import annotations

from typing import TYPE_CHECKING

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
    manifest = project.state.load_run(run_id)
    sources = [
        source
        for document in project.state.portable_run_export(run_id).files
        if (source := _portable(document)) is not None
    ]
    store = project.state.state_store_namespace(run_id).read_bytes("store.json")
    if store is not None:
        sources.append(ImportSource("store/store.json", SourceFormat.PROJECT_STORE, store))
    for document in project.state.local_namespace(run_id, "receipts").documents():
        path = document.relative_path.as_posix()
        if path == "receipts.lock":
            continue
        sources.append(ImportSource(f"receipts/{path}", SourceFormat.RECEIPT, document.contents))
    sessions = project.state.local_namespace(run_id, "agent").read_bytes("sessions.json")
    if sessions is not None:
        sources.append(ImportSource("agent/sessions.json", SourceFormat.SESSIONS, sessions))
    for entry in project.state.control_inbox(run_id).pending():
        if entry.unreadable is not None:
            raise FactImportError(f"control/inbox/{entry.request_id}.json", entry.unreadable)
        sources.append(
            ImportSource(
                f"control/inbox/{entry.request_id}.json", SourceFormat.CONTROL, entry.payload
            )
        )
    journal = project.event_journal_path_for(project.root, run_id)
    if journal.exists():
        if journal.is_symlink() or not journal.is_file():
            raise FactImportError("core-events.jsonl", "regular retained journal required")
        with journal.open("rb") as stream:
            data = stream.read(MAX_SOURCE_BYTES + 1)
        sources.append(ImportSource("core-events.jsonl", SourceFormat.EVENTS, data))
    return import_run_facts(run_id, sources, strategy=manifest.orchestration.id, registry=registry)
