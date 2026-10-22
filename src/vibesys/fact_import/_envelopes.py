"""Cross-field invariants of retained containers; no scheduling state is reconstructed."""

from __future__ import annotations

from typing import Any

from vs_agent.api import AgentSessionKey

from ._descriptors import OLDEST_ENVELOPE_VERSION, envelope_versions
from ._source import FactImportError, ImportSource


def validate_container(
    source: ImportSource,
    path: str,
    name: str,
    value: dict[str, Any],
    identity: tuple[str, str],
) -> None:
    run_id, strategy = identity
    if name == "flow":
        if value["schema_version"] != 1:
            raise FactImportError(
                f"{source.path}:{path}.schema_version", "unknown retained FlowState version"
            )
    elif name == "envelope":
        _envelope(source, path, value, run_id, strategy)
    elif name == "runtime":
        _envelope(source, f"{path}.envelope", value["envelope"], run_id, strategy)
        cursor = value["envelope"]["event_cursor"]["sequence"]
        if value["next_publication_sequence"] != cursor + 1:
            raise FactImportError(
                f"{source.path}:{path}.next_publication_sequence", "must follow event cursor"
            )
        _publications(
            source, path, value["pending_publications"], run_id, value["delivery_cursor"] + 1
        )
        if len(value["pending_publications"]) != cursor - value["delivery_cursor"]:
            raise FactImportError(
                f"{source.path}:{path}.pending_publications", "publication sequence gap"
            )
    elif name == "publications":
        _publications(source, path, value["publications"], run_id, 1)
    elif name == "sessions":
        _sessions(source, path, value)


def _sessions(source: ImportSource, path: str, value: dict[str, Any]) -> None:
    for key in value.get("sessions", {}):
        try:
            identity = AgentSessionKey.parse(key)
        except ValueError as error:
            raise FactImportError(f"{source.path}:{path}.sessions.{key}", str(error)) from error
        if not identity.durable:
            raise FactImportError(
                f"{source.path}:{path}.sessions.{key}", "session scope is not persistable"
            )


def _envelope(
    source: ImportSource, path: str, value: dict[str, Any], run_id: str, strategy: str
) -> None:
    if value["schema_version"] not in range(OLDEST_ENVELOPE_VERSION, envelope_versions()[-1] + 1):
        raise FactImportError(
            f"{source.path}:{path}.schema_version", "unknown retained envelope version"
        )
    if value["core"]["run"]["run_id"]["root"] != run_id:
        raise FactImportError(
            f"{source.path}:{path}.core.run.run_id", "source belongs to another run"
        )
    if value["strategy"]["schema_version"] != 1:
        raise FactImportError(
            f"{source.path}:{path}.strategy.schema_version", "unknown retained FlowState version"
        )
    if value["state_schema"] != {
        "name": f"{value['strategy_id']['root']}.flow-state",
        "version": value["strategy"]["schema_version"],
    }:
        raise FactImportError(f"{source.path}:{path}.state_schema", "strategy declaration mismatch")
    if value["strategy_id"] != value["core"]["run"]["declaration"]["strategy_id"]:
        raise FactImportError(f"{source.path}:{path}.strategy_id", "strategy declaration mismatch")
    if value["strategy_id"]["root"] != strategy:
        raise FactImportError(
            f"{source.path}:{path}.strategy_id", "source belongs to another strategy"
        )


def _publications(
    source: ImportSource, path: str, rows: list[dict[str, Any]], run_id: str, start: int
) -> None:
    for sequence, row in enumerate(rows, start=start):
        if row["sequence"] != sequence or row["publication_id"] != f"{run_id}:{sequence}":
            raise FactImportError(
                f"{source.path}:{path}.publications",
                "publication sequence gap or identity conflict",
            )
