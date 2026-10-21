"""Original run configuration from retained manifest versions, without upgrading it."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from pydantic import JsonValue, TypeAdapter

from vs_project.api import OrchestrationRunManifest
from vs_proto.api import imported_facts as pb

from ._facts import content
from ._snapshots import _reader
from ._source import FactImportError, decode

if TYPE_CHECKING:
    from vs_core.api import OperationRegistry

    from ._source import ImportSource

_CURRENT_VERSION = 5
_LOOP_VERSION = 3


def read_manifest(
    source: ImportSource, registry: OperationRegistry
) -> tuple[str, pb.ImportedInput, dict[str, JsonValue] | None]:
    """Return the recorded strategy, original input and versioned launch options."""
    value = decode(source, TypeAdapter(dict[str, JsonValue]), registry)
    version = value.get("schema_version")
    if version == _CURRENT_VERSION:
        manifest = decode(source, TypeAdapter(OrchestrationRunManifest), registry)
        strategy = manifest.orchestration.id
        options = manifest.orchestration.options
        config_version = manifest.orchestration.config_version
    elif version in (3, 4):
        reader = _reader("single", f"manifest-v{version}")
        error = next(reader.iter_errors(value), None)
        if error is not None:
            path = ".".join(str(part) for part in error.absolute_path)
            raise FactImportError(f"{source.path}:$.{path}", error.message)
        if version == _LOOP_VERSION:
            configuration = cast("dict[str, JsonValue]", value["configuration"])
            loop = str(configuration["outer_loop"])
            strategy = (
                str(configuration["inner_loop"]) if loop in ("agent", "profile-guided") else loop
            )
            if loop == "profile-guided":
                strategy = f"profile-guided-{strategy}"
            options = None
            config_version = 1
        else:
            orchestration = cast("dict[str, JsonValue]", value["orchestration"])
            strategy = str(orchestration["id"])
            options = cast("dict[str, JsonValue]", orchestration["options"])
            config_version = orchestration["config_version"]
    else:
        raise FactImportError(
            f"{source.path}:$.schema_version", "unknown retained manifest version"
        )
    if config_version != 1:
        raise FactImportError(
            f"{source.path}:$.orchestration.config_version", "unknown retained config version"
        )
    return (
        strategy,
        pb.ImportedInput(
            baseline_revision=str(value["trusted_input_baseline"]),
            input_fingerprint=str(value["input_fingerprint"]),
            config=[content(source.data, "application/vnd.vibesys.legacy-manifest+json")],
        ),
        options,
    )
