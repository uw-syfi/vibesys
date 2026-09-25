"""Host-owned turn artifacts: role-handoff data written to disk generically.

A strategy's designer, implementer, and judge roles hand off large evidence
(a plan, an implementer's parsed claims, framework-executed validation
results) through files rather than prompt text, so a later turn can inspect
only what it needs with tools instead of receiving everything inline. This
module is where that JSON gets written: every artifact lives under the
run's structured artifact root (:func:`vibesys.orchestration.memory.structured_artifact_root`,
a directory beside -- or, for legacy ``progress.md`` runs, a sibling of --
the progress log), one category subdirectory per artifact kind (``plans``,
``evidence``, ``validation``, ``profiles/round-NNNN``).

``write_json`` is the one atomic-write primitive every writer in this module
(and :func:`write_validation_recipe_schema`, whose payload is a JSON schema
rather than a model instance) goes through. This module knows nothing about
*which* pydantic model a plan or an implementer reply is -- those types live
one layer above the host (``vibesys.search.hypothesis``, ``vibesys.roles``)
-- so it writes any :class:`~pydantic.BaseModel` via :func:`write_model` and
only computes the destination path; the caller in ``loops/`` supplies the
typed value.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel

from vibesys.evaluators.validation_recipe import (
    FrameworkValidationResult,  # tracked: #288
    ValidationRecipeArtifact,
)
from vibesys.orchestration.memory import structured_artifact_root


def write_json(path: Path, payload: object) -> Path:
    """Atomically replace a framework-owned JSON handoff artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def write_model(path: Path, model: BaseModel) -> Path:
    """Atomically write one typed artifact, as its ``mode="json"`` dump."""
    return write_json(path, model.model_dump(mode="json"))


def plan_artifact_path(progress_path: Path, round_number: int) -> Path:
    """Return the destination for one round's typed plan artifact.

    The caller (``loops/``) owns the plan's type (``OrchestratorPlan``) and
    writes it with :func:`write_model`; this module only knows the path.
    """
    return structured_artifact_root(progress_path) / "plans" / f"round-{round_number:04d}.json"


#: Name tail of a completed implementer attempt artifact.
_IMPLEMENTER_ARTIFACT_SUFFIX = "-implementer.json"
#: Name tail of an attempt's start marker. The completed-artifact glob requires
#: the exact ``-implementer.json`` tail, so a marker never reads back as a
#: completed attempt.
_IMPLEMENTER_START_MARKER_SUFFIX = "-implementer.started.json"


def _implementer_evidence_root(progress_path: Path) -> Path:
    """Return the framework-owned directory of per-attempt implementer evidence."""
    return structured_artifact_root(progress_path) / "evidence"


def implementer_artifact_path(progress_path: Path, round_number: int, retry: int) -> Path:
    """Return the destination for one attempt's implementer evidence artifact.

    The caller (``loops/``) owns the reply's type (``ImplementerReply``) and
    writes it with :func:`write_model`; this module only knows the path.
    """
    return _implementer_evidence_root(progress_path) / (
        f"round-{round_number:04d}-attempt-{retry:02d}{_IMPLEMENTER_ARTIFACT_SUFFIX}"
    )


class ImplementerStartMarker(BaseModel):
    """Crash-safety marker: one implementer attempt began, before its turn runs."""

    round: int
    attempt: int


def write_implementer_start_marker(progress_path: Path, round_number: int, retry: int) -> Path:
    """Record that one implementer attempt began, before its turn runs."""
    path = _implementer_evidence_root(progress_path) / (
        f"round-{round_number:04d}-attempt-{retry:02d}{_IMPLEMENTER_START_MARKER_SUFFIX}"
    )
    return write_model(path, ImplementerStartMarker(round=round_number, attempt=retry))


def validation_artifact_root(progress_path: Path) -> Path:
    """Return the framework-owned validation ledger directory."""
    return structured_artifact_root(progress_path) / "validation"


def profiler_artifact_root(progress_path: Path, round_number: int) -> Path:
    """Return the only durable output directory writable by a Profiler turn."""
    return structured_artifact_root(progress_path) / "profiles" / f"round-{round_number:04d}"


def validation_recipe_schema_path(progress_path: Path) -> Path:
    """Return the framework-owned candidate recipe-schema path."""
    return validation_artifact_root(progress_path) / "recipe-schema.json"


def write_validation_recipe_schema(progress_path: Path) -> Path:
    """Publish the authoritative recipe contract for on-demand agent reads."""
    return write_json(
        validation_recipe_schema_path(progress_path),
        ValidationRecipeArtifact.model_json_schema(mode="validation"),
    )


def write_validation_result_artifact(
    progress_path: Path,
    round_number: int,
    retry: int,
    results: list[FrameworkValidationResult],
) -> Path:
    """Persist framework-executed validation results for replay and reuse."""
    path = (
        validation_artifact_root(progress_path)
        / f"round-{round_number:04d}-attempt-{retry:02d}.json"
    )
    payload = {
        "round": round_number,
        "attempt": retry,
        "results": [result.model_dump(mode="json") for result in results],
    }
    return write_json(path, payload)


def validation_result_artifact_paths(progress_path: Path) -> list[Path]:
    """Return validation result artifacts in deterministic creation order."""
    return sorted(validation_artifact_root(progress_path).glob("round-*-attempt-*.json"))


def implementer_artifact_paths(progress_path: Path, round_number: int) -> list[Path]:
    """Return persisted implementer attempts for one round in attempt order."""
    pattern = f"round-{round_number:04d}-attempt-*{_IMPLEMENTER_ARTIFACT_SUFFIX}"
    return sorted(_implementer_evidence_root(progress_path).glob(pattern))


def _implementer_attempt_numbers(progress_path: Path, round_number: int, suffix: str) -> list[int]:
    """Return the attempt numbers named by one round's *suffix* evidence files."""
    prefix = f"round-{round_number:04d}-attempt-"
    names = _implementer_evidence_root(progress_path).glob(f"{prefix}*{suffix}")
    attempts = (path.name.removeprefix(prefix).removesuffix(suffix) for path in names)
    return [int(attempt) for attempt in attempts if attempt.isdigit()]


def next_implementer_attempt(progress_path: Path, round_number: int) -> int:
    """Return the next durable attempt number for an interrupted round.

    Start markers count alongside completed artifacts, which makes the attempt
    number durable at attempt start rather than only once the turn returns. A
    process killed mid-invoke therefore resumes on a fresh attempt instead of
    replaying the killed attempt's round label.
    """
    attempts = [
        attempt
        for suffix in (_IMPLEMENTER_ARTIFACT_SUFFIX, _IMPLEMENTER_START_MARKER_SUFFIX)
        for attempt in _implementer_attempt_numbers(progress_path, round_number, suffix)
    ]
    return max(attempts, default=0) + 1
