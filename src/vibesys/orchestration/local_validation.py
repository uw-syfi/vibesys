"""Candidate-authored local validation executed as a runtime mechanism."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from vibesys.evaluators.gates import (
    GATE_LOG_TAIL_CHARS,
    GATE_RECORD_TAIL_CHARS,
    emit_gate_finished,
    emit_gate_started,
)
from vibesys.evaluators.validation_recipe import (
    FrameworkValidationResult,
    ValidationRecipe,
    ValidationRecipeArtifact,
)
from vibesys.events import GateFinishedData, GateKind
from vibesys.orchestration.artifacts import write_json
from vs_runtime.api import LocalValidationEvaluation

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.orchestration._host import HostResources
    from vibesys.orchestration.workspaces import WorkspaceHandle

_MAX_INPUT_FILES = 4096
_MAX_INPUT_BYTES = 256 * 1024 * 1024


def _workspace_path(workspace: Path, relative: str, *, kind: str) -> Path:
    """Resolve a validated workspace-relative path without following an escape."""
    root = workspace.resolve()
    path = (workspace / relative).resolve()
    if not path.is_relative_to(root):
        message = f"{kind} escapes the workspace"
        raise ValueError(message)
    return path


def _load_recipes(workspace: Path, artifact: str) -> list[ValidationRecipe]:
    """Read the strict candidate-authored recipe artifact."""
    path = _workspace_path(workspace, artifact, kind="validation recipe artifact")
    if not path.is_file():
        message = f"validation recipe artifact does not exist: {artifact}"
        raise ValueError(message)
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        message = f"validation recipe artifact is not valid JSON: {exc}"
        raise ValueError(message) from exc
    try:
        return ValidationRecipeArtifact.model_validate(payload).recipes
    except (TypeError, ValueError) as exc:
        message = f"validation recipe artifact does not match version 1: {exc}"
        raise ValueError(message) from exc


def _input_digest(workspace: Path, recipe: ValidationRecipe) -> str:
    """Hash the bounded declared inputs that determine safe pass reuse."""
    digest = hashlib.sha256()
    root = workspace.resolve()
    total_files = 0
    total_bytes = 0
    for relative in sorted(recipe.input_paths):
        unresolved = workspace / relative
        if unresolved.is_symlink():
            message = f"validation input must not be a symlink: {relative}"
            raise ValueError(message)
        path = unresolved.resolve()
        if not path.is_relative_to(root):
            message = f"validation input escapes workspace: {relative}"
            raise ValueError(message)
        if not path.exists():
            message = f"validation input does not exist: {relative}"
            raise ValueError(message)
        digest.update(relative.encode())
        digest.update(b"\0dir\0" if path.is_dir() else b"\0file\0")
        entries = [path]
        if path.is_dir():
            entries = sorted(candidate for candidate in path.rglob("*") if candidate.is_file())
        for entry in entries:
            if entry.is_symlink():
                message = f"validation input must not be a symlink: {relative}"
                raise ValueError(message)
            total_files += 1
            total_bytes += entry.stat().st_size
            if total_files > _MAX_INPUT_FILES or total_bytes > _MAX_INPUT_BYTES:
                message = "validation inputs exceed the 4096-file/256-MiB reuse-hash limit"
                raise ValueError(message)
            digest.update(entry.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(entry.read_bytes())
            digest.update(b"\0")
    digest.update(recipe.command.encode())
    digest.update(b"\0")
    digest.update(str(recipe.timeout_seconds).encode("ascii"))
    return digest.hexdigest()


def _reusable_result(
    report: Path,
    recipe: ValidationRecipe,
    input_digest: str,
) -> FrameworkValidationResult | None:
    """Return the newest exact matching pass from the report directory."""
    if not report.parent.exists():
        return None
    for artifact in sorted(report.parent.glob("*.json"), reverse=True):
        if artifact == report:
            continue
        try:
            payload = json.loads(artifact.read_text())
            raw_results = payload.get("results", [])
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        for raw in reversed(raw_results):
            try:
                result = FrameworkValidationResult.model_validate(raw)
            except (TypeError, ValueError):
                continue
            if result.passed and result.input_digest == input_digest and result.recipe == recipe:
                return result.model_copy(update={"reused": True})
    return None


async def _execute_recipe(
    host: HostResources,
    workspace: WorkspaceHandle,
    recipe: ValidationRecipe,
    input_digest: str,
) -> tuple[FrameworkValidationResult, bool]:
    """Execute one recipe and convert execution or mutation into a result."""
    try:
        execution = await host.environment.execute(
            recipe.command,
            timeout_seconds=recipe.timeout_seconds,
            scope=workspace,
        )
        output = execution.output.strip()
        result = FrameworkValidationResult(
            recipe=recipe,
            input_digest=input_digest,
            passed=execution.exit_code == 0,
            exit_code=execution.exit_code,
            output=output[-GATE_RECORD_TAIL_CHARS:],
            error=None if execution.exit_code == 0 else "command exited nonzero",
        )
    except Exception as error:  # noqa: BLE001  # lint-waiver: LW-020039 [BLE001]; execution failures are evaluation feedback, not host failures.
        result = FrameworkValidationResult(
            recipe=recipe,
            input_digest=input_digest,
            passed=False,
            error=f"command could not be executed: {error}",
        )
    changes = await workspace.pending_changes()
    if not changes:
        return result, False
    return (
        result.model_copy(
            update={
                "passed": False,
                "error": f"validation command mutated the workspace: {', '.join(changes[:8])}",
            }
        ),
        True,
    )


async def validate_local(
    host: HostResources,
    workspace: WorkspaceHandle,
    *,
    recipe_artifact: str,
    report_location: str,
) -> LocalValidationEvaluation:
    """Execute audited recipes, restore mutations, and persist a reusable report."""
    try:
        recipes = _load_recipes(workspace.path, recipe_artifact)
        report = _workspace_path(
            workspace.path,
            report_location,
            kind="local validation report location",
        )
    except ValueError as error:
        return LocalValidationEvaluation(
            passed=False,
            feedback=f"Framework local validation recipe error: {error}.",
        )
    names = [recipe.name for recipe in recipes]
    if len(names) != len(set(names)):
        return LocalValidationEvaluation(
            passed=False,
            feedback="Framework local validation recipes contain duplicate names.",
        )

    results: list[FrameworkValidationResult] = []
    restore_required = False
    async with workspace.transaction(label="framework-local-validation-input") as transaction:
        for recipe in recipes:
            try:
                digest = _input_digest(workspace.path, recipe)
            except (OSError, ValueError) as error:
                results.append(
                    FrameworkValidationResult(
                        recipe=recipe,
                        input_digest="",
                        passed=False,
                        error=str(error),
                    )
                )
                break
            reused = _reusable_result(report, recipe, digest)
            emit_gate_started(GateKind.VALIDATION, recipe=recipe.name, command=recipe.command)
            if reused is not None:
                results.append(reused)
                emit_gate_finished(
                    GateFinishedData(gate=GateKind.VALIDATION, recipe=recipe.name, reused=True),
                    passed=True,
                )
                continue
            result, mutated = await _execute_recipe(host, workspace, recipe, digest)
            restore_required = restore_required or mutated
            results.append(result)
            failure = (
                None if result.passed else (result.error or result.output or "unknown failure")
            )
            emit_gate_finished(
                GateFinishedData(
                    gate=GateKind.VALIDATION,
                    recipe=recipe.name,
                    output_tail=None if failure is None else failure[-GATE_LOG_TAIL_CHARS:],
                ),
                passed=result.passed,
            )
            if not result.passed:
                break
        if not restore_required:
            transaction.commit()

    write_json(
        report,
        {
            "version": 1,
            "results": [result.model_dump(mode="json") for result in results],
        },
    )
    await workspace.snapshot("framework-local-validation")
    failed = next((result for result in results if not result.passed), None)
    if failed is None:
        return LocalValidationEvaluation(passed=True, report_location=report_location)
    detail = failed.error or failed.output or "unknown failure"
    return LocalValidationEvaluation(
        passed=False,
        feedback=(
            f"Framework local validation failed for {failed.recipe.name!r}: {detail}. "
            f"Inspect `{report_location}` and repair only the affected local contract."
        ),
        report_location=report_location,
    )
