"""Thin read-only Project shell for the one pure facts importer.

Only imported paths are read. A retained file outside every imported format, such as a
large log or profile under a namespace this importer does not lower, is ignored without
reading its bytes. Each read is bounded, and the selected files, journal included, have a
total cap that is checked from their listed sizes before any of them is read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.fact_import.api import (
    MAX_SOURCE_BYTES,
    FactImportError,
    ImportSource,
    SourceFormat,
    import_run_facts,
    manifest_strategy,
)
from vs_project.api import StateDocumentDamagedError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from vs_core.api import OperationRegistry
    from vs_project.api import Project
    from vs_project.api.retained import RetainedFile
    from vs_proto.api import imported_facts as pb

__all__ = ["RetainedReadLimits", "read_run_facts"]


@dataclass(frozen=True, slots=True)
class RetainedReadLimits:
    """The largest retained document, and selected source set, one import reads."""

    document_bytes: int = MAX_SOURCE_BYTES
    run_bytes: int = 4 * MAX_SOURCE_BYTES

    def __post_init__(self) -> None:
        """Reject a document limit that the run limit or the importer would refuse."""
        if not 0 < self.document_bytes <= min(self.run_bytes, MAX_SOURCE_BYTES):
            message = "document_bytes must be positive and within run_bytes and the source limit"
            raise ValueError(message)


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


def _portable_format(document: RetainedFile) -> SourceFormat | None:
    path = document.relative_path.as_posix()
    if path in _FORMATS:
        return _FORMATS[path]
    if document.relative_path.name == "state.json":
        raise FactImportError(path, "unknown retained state format")
    if document.relative_path.parts[0] in _CONTENT_NAMESPACES:
        return SourceFormat.CONTENT
    return None


class _Selection:
    """Selected retained files, read only after the whole set fits the total cap."""

    def __init__(self, limits: RetainedReadLimits) -> None:
        self._limits = limits
        self._chosen: list[tuple[str, SourceFormat, RetainedFile]] = []

    def add(self, path: str, source_format: SourceFormat, document: RetainedFile) -> None:
        if document.size > self._limits.document_bytes:
            raise FactImportError(path, "retained document exceeds byte limit")
        self._chosen.append((path, source_format, document))

    def read(self, journal_bytes: int) -> list[ImportSource]:
        total = journal_bytes + sum(document.size for _, _, document in self._chosen)
        if total > self._limits.run_bytes:
            raise FactImportError("<run>", "selected retained documents exceed run byte limit")
        return [
            ImportSource(path, source_format, self._read(path, document))
            for path, source_format, document in self._chosen
        ]

    def _read(self, path: str, document: RetainedFile) -> bytes:
        try:
            return document.read(max_bytes=self._limits.document_bytes)
        except StateDocumentDamagedError as error:
            raise FactImportError(path, str(error)) from error


def _local(project: Project, run_id: str, namespace: str) -> Iterable[tuple[str, RetainedFile]]:
    for document in project.state.retained_run_files(run_id, local_namespace=namespace):
        yield document.relative_path.as_posix(), document


def _select(project: Project, run_id: str, selection: _Selection) -> None:
    for document in project.state.retained_run_files(run_id):
        if (source_format := _portable_format(document)) is not None:
            selection.add(document.relative_path.as_posix(), source_format, document)
    for path, document in _local(project, run_id, "receipts"):
        if path != "receipts.lock":
            selection.add(f"receipts/{path}", SourceFormat.RECEIPT, document)
    for path, document in _local(project, run_id, "agent"):
        if path == "sessions.json":
            selection.add("agent/sessions.json", SourceFormat.SESSIONS, document)
    controls = list(_local(project, run_id, "control"))
    settled = {
        document.relative_path.name
        for _, document in controls
        if document.relative_path.parts[0] in ("settled", "rejected")
    }
    for path, document in controls:
        if (
            document.relative_path.parts[0] == "inbox"
            and document.relative_path.name not in settled
        ):
            selection.add(f"control/{path}", SourceFormat.CONTROL, document)


def read_run_facts(
    project: Project,
    run_id: str,
    *,
    registry: OperationRegistry,
    limits: RetainedReadLimits | None = None,
) -> pb.ImportedRunFacts:
    """Read a stopped run through Project and import it without changing a single file.

    This function grants no exclusion or dispatch authority. Its caller must stop old
    writers before taking this source set and keep exclusive ownership until publishing
    the new epoch. It reads original retained files, never the user's edited task root.
    """
    limits = RetainedReadLimits() if limits is None else limits
    selection = _Selection(limits)
    _select(project, run_id, selection)
    try:
        journal = project.retained_event_journal(run_id, max_bytes=limits.document_bytes)
    except StateDocumentDamagedError as error:
        raise FactImportError("core-events.jsonl", str(error)) from error
    sources = selection.read(0 if journal is None else len(journal))
    manifest = next((source for source in sources if source.format == SourceFormat.MANIFEST), None)
    if manifest is None:
        raise FactImportError("run.json", "original run manifest is required")
    if journal is not None:
        sources.append(ImportSource("core-events.jsonl", SourceFormat.EVENTS, journal))
    strategy = manifest_strategy(manifest, registry=registry)
    return import_run_facts(run_id, sources, strategy=strategy, registry=registry)
