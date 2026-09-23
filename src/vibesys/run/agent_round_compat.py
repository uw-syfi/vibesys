"""Compatibility store for completed rounds from older agent runs.

New agent runs persist ``agent/state.json``. This store owns the former
``agent/rounds/NNNN.json`` interpretation and the exact bytes needed to
recover version 3 round journals without teaching ``vs_project`` agent policy.
"""

# Boundary diagnostics include the offending round and state path.
# ruff: noqa: TRY003

from __future__ import annotations

import json
import math
import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from pydantic import ValidationError

from vs_loop_state.api import RoundRecord, parse_round_record, serialize_round_record
from vs_project.api import ProjectStateError

if TYPE_CHECKING:
    from vs_project.api import Project, StateSnapshot

_ROUND_FILE_PATTERN = re.compile(r"^(?P<round>0*[1-9][0-9]*)\.json$")


class LegacyAgentRoundStore:
    """Read and repair the former portable agent completed-round files."""

    def __init__(self, project: Project, run_id: str) -> None:
        """Bind an existing run's portable agent namespace."""
        self._namespace = project.state.portable_namespace(run_id, "agent")

    def load(self) -> list[RoundRecord]:
        """Load a contiguous completed-round history in round order."""
        numbered = self._numbered_files()
        records: list[RoundRecord] = []
        for expected, (number, path, contents) in enumerate(numbered, start=1):
            if number != expected:
                raise ProjectStateError(
                    "Completed rounds must form a contiguous sequence starting at 1: "
                    f"expected round {expected}, found {number} at {self._display(path)}"
                )
            record = self._parse(contents, path)
            if record.round_number != number:
                raise ProjectStateError(
                    f"Round file {self._display(path)} contains round {record.round_number}, "
                    f"expected {number}"
                )
            records.append(record)
        return records

    def prepare_snapshot(self, record: RoundRecord) -> StateSnapshot:
        """Prepare the canonical file snapshot without writing it."""
        return self._namespace.snapshot_bytes(
            self._path(record.round_number), self._serialize(record)
        )

    def save(self, record: RoundRecord) -> StateSnapshot:
        """Append a round, or return its existing exact snapshot if equal."""
        completed = self.load()
        path = self._path(record.round_number)
        contents = self._serialize(record)
        if record.round_number <= len(completed):
            if completed[record.round_number - 1] != record:
                raise ProjectStateError(
                    f"Completed round already exists with different data: {self._display(path)}"
                )
            existing = self._namespace.read_bytes(path)
            if existing is None:
                raise ProjectStateError(f"Completed round does not exist: {self._display(path)}")
            return self._namespace.snapshot_bytes(path, existing)
        expected = len(completed) + 1
        if record.round_number != expected:
            raise ProjectStateError(
                f"Completed rounds must be appended in order: expected round {expected}, "
                f"got {record.round_number}"
            )
        self._namespace.write_bytes(path, contents)
        return self._namespace.snapshot_bytes(path, contents)

    def restore(self, record: RoundRecord) -> StateSnapshot:
        """Repair a journaled round while requiring valid predecessors."""
        contents = self._serialize(record)
        numbered = self._numbered_files()
        later = [number for number, _, _ in numbered if number > record.round_number]
        if later:
            raise ProjectStateError(
                f"Cannot restore round {record.round_number} before existing round {min(later)}"
            )
        predecessors = {number: (path, payload) for number, path, payload in numbered}
        for number in range(1, record.round_number):
            previous = predecessors.get(number)
            if previous is None:
                raise ProjectStateError(
                    f"Cannot restore round {record.round_number} without completed round {number}"
                )
            path, payload = previous
            parsed = self._parse(payload, path)
            if parsed.round_number != number:
                raise ProjectStateError(
                    f"Round file {self._display(path)} contains round {parsed.round_number}, "
                    f"expected {number}"
                )
        path = self._path(record.round_number)
        self._namespace.write_bytes(path, contents)
        return self._namespace.snapshot_bytes(path, contents)

    def _numbered_files(self) -> list[tuple[int, PurePosixPath, bytes]]:
        files: list[tuple[int, PurePosixPath, bytes]] = []
        seen: dict[int, PurePosixPath] = {}
        for name in self._namespace.entries("rounds"):
            path = PurePosixPath("rounds") / name
            match = _ROUND_FILE_PATTERN.fullmatch(name)
            if match is None:
                raise ProjectStateError(f"Unexpected completed-round entry: {self._display(path)}")
            number = int(match.group("round"))
            if number in seen:
                raise ProjectStateError(
                    f"Duplicate completed-round number {number}: "
                    f"{self._display(seen[number])} and {self._display(path)}"
                )
            seen[number] = path
            contents = self._namespace.read_bytes(path)
            if contents is None:
                raise ProjectStateError(f"Completed round does not exist: {self._display(path)}")
            files.append((number, path, contents))
        return sorted(files, key=lambda item: item[0])

    def _parse(self, contents: bytes, path: PurePosixPath) -> RoundRecord:
        try:
            payload = json.loads(contents)
        except (TypeError, ValueError) as exc:
            raise ProjectStateError(
                f"Invalid completed-round metadata at {self._display(path)}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise ProjectStateError(
                f"Invalid completed-round metadata at {self._display(path)}: "
                "payload must be a JSON object"
            )
        try:
            record = parse_round_record(payload)
        except (TypeError, ValueError, ValidationError) as exc:
            raise ProjectStateError(
                f"Invalid completed-round metadata at {self._display(path)}: {exc}"
            ) from exc
        self._validate(record, source=self._display(path))
        return record

    @staticmethod
    def _validate(record: RoundRecord, *, source: str) -> None:
        for field_name in ("evaluation_artifact", "candidate_evaluation_artifact"):
            value = getattr(record, field_name)
            if value is None:
                continue
            artifact = PurePosixPath(value)
            if (
                not value
                or "\\" in value
                or artifact.is_absolute()
                or artifact == PurePosixPath(".")
                or ".." in artifact.parts
            ):
                raise ProjectStateError(
                    f"Completed-round metadata at {source} {field_name} "
                    "must be a portable project-relative path"
                )
        metrics = [record.perf_metric, *record.metrics.values(), *record.candidate_metrics.values()]
        if any(value is not None and not math.isfinite(value) for value in metrics):
            raise ProjectStateError(
                f"Completed-round metadata at {source} metrics must be finite numbers"
            )

    def _serialize(self, record: RoundRecord) -> bytes:
        if record.round_number < 1:
            raise ProjectStateError(f"Round number must be positive, got {record.round_number}")
        self._validate(record, source=self._display(self._path(record.round_number)))
        try:
            content = json.dumps(
                serialize_round_record(record), allow_nan=False, indent=2, sort_keys=True
            )
        except (TypeError, ValueError) as exc:
            raise ProjectStateError(
                f"Could not serialize completed-round metadata for round {record.round_number}"
            ) from exc
        return f"{content}\n".encode()

    @staticmethod
    def _path(round_number: int) -> PurePosixPath:
        return PurePosixPath("rounds") / f"{round_number:04d}.json"

    def _display(self, path: PurePosixPath) -> str:
        return self._namespace.agent_visible_path(path)
