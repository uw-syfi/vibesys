"""Frozen positional snapshot readers, independent of live strategies and their code."""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import AwareDatetime, JsonValue, TypeAdapter

from vs_core.api import (
    ChargeReceipt,
    ChildLease,
    EvidenceRef,
    InputRecord,
    OperationSchemaRef,
    OwnedJob,
    RegisteredOwnedJob,
    RequestRecord,
    RunFacts,
)
from vs_core.api.values import ArtifactRef, RevisionRef, Sample

from ._descriptors import descriptor
from ._envelopes import validate_container
from ._legacy_lower import lower_legacy_value
from ._rounds import lower_round, read_round
from ._source import FactImportError, decode

if TYPE_CHECKING:
    from vs_core.api import OperationRegistry

    from ._facts import FactSet
    from ._source import ImportSource

_JSON = TypeAdapter(JsonValue)
_FORMATS = FormatChecker()


@_FORMATS.checks("date-time", raises=ValueError)
def _aware_datetime(value: object) -> bool:
    TypeAdapter(AwareDatetime).validate_python(value)
    return True


_TYPES: dict[str, TypeAdapter[Any]] = {
    "RequestRecord": TypeAdapter(RequestRecord),
    "ChildLease": TypeAdapter(ChildLease),
    "OwnedJob": TypeAdapter(OwnedJob),
    "RegisteredOwnedJob": TypeAdapter(RegisteredOwnedJob),
    "ArtifactRef": TypeAdapter(ArtifactRef),
    "RevisionRef": TypeAdapter(RevisionRef),
    "Sample": TypeAdapter(Sample),
    "EvidenceRef": TypeAdapter(EvidenceRef),
    "ChargeReceipt": TypeAdapter(ChargeReceipt),
    "InputRecord": TypeAdapter(InputRecord),
    "RunFacts": TypeAdapter(RunFacts),
}
_STRATEGIES = {
    "single": "single",
    "multi": "multi",
    "dynamic": "dynamic",
    "evolve": "evolve",
    "plain": "plain",
    "single-agent": "single",
    "profile-guided-single-agent": "single",
    "multi-agent": "multi",
    "profile-guided-multi-agent": "multi",
}


@cache
def _reader(strategy: str, name: str, envelope_version: int | None) -> Draft202012Validator:
    try:
        schema = descriptor(f"shared:{name}", envelope_version)
    except KeyError:
        schema = descriptor(f"{_STRATEGIES[strategy]}:{name}", envelope_version)
    return Draft202012Validator(schema, format_checker=_FORMATS)


def _envelope_version(name: str, value: JsonValue) -> int | None:
    """The envelope version a container names, before its descriptor validates it."""
    envelope = value.get("envelope") if name == "runtime" and isinstance(value, dict) else value
    version = envelope.get("schema_version") if isinstance(envelope, dict) else None
    return version if name in ("envelope", "runtime") and isinstance(version, int) else None


class SnapshotDecoder:
    """Validate frozen source shapes, then lower only recognized shared fact nodes."""

    def __init__(
        self, registry: OperationRegistry, facts: FactSet, strategy: str, run_id: str
    ) -> None:
        self.registry = registry
        self.facts = facts
        self.strategy = strategy
        self.run_id = run_id

    def read(
        self, source: ImportSource, selector: str, data: bytes | str, name: str | None
    ) -> None:
        if name is None:
            decode(source, TypeAdapter(type(None)), self.registry, selector=selector, data=data)
            return
        value = decode(source, _JSON, self.registry, selector=selector, data=data)
        try:
            reader = _reader(self.strategy, name, _envelope_version(name, value))
        except KeyError as error:
            raise FactImportError(
                f"{source.path}:{selector}", f"unknown retained snapshot {self.strategy}/{name}"
            ) from error
        error = next(reader.iter_errors(value), None)
        if error is not None:
            path = ".".join(str(part) for part in error.absolute_path)
            raise FactImportError(f"{source.path}:{selector}.{path}", error.message)
        if isinstance(value, dict):
            validate_container(
                source,
                selector,
                name,
                value,
                (self.run_id, _STRATEGIES.get(self.strategy, self.strategy)),
            )
            if name == "operation-result":
                self._embedded(source, selector, "OperationResult", value)
        self._visit(source, selector, value, reader, reader.schema)

    def _visit(
        self,
        source: ImportSource,
        path: str,
        value: JsonValue,
        reader: Draft202012Validator,
        root: dict[str, Any],
    ) -> None:
        schema = reader.schema
        if reference := schema.get("$ref"):
            name = reference.rsplit("/", 1)[-1].rsplit("__", 1)[-1]
            self._fact(source, path, name, value)
            target = root["$defs"][reference.rsplit("/", 1)[-1]]
            self._visit(source, path, value, reader.evolve(schema=target), root)
        elif isinstance(value, dict):
            properties = schema.get("properties", {})
            for name, item in value.items():
                child = properties.get(name, schema.get("additionalProperties"))
                if isinstance(child, dict):
                    self._visit(source, f"{path}.{name}", item, reader.evolve(schema=child), root)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                prefixes = schema.get("prefixItems", [])
                child = prefixes[index] if index < len(prefixes) else schema.get("items")
                if isinstance(child, dict):
                    self._visit(source, f"{path}[{index}]", item, reader.evolve(schema=child), root)
        for variant in schema.get("anyOf", schema.get("oneOf", [])):
            branch = reader.evolve(schema=variant)
            if branch.is_valid(value):
                self._visit(source, path, value, branch, root)

    def _fact(self, source: ImportSource, path: str, name: str, value: JsonValue) -> None:
        if source.format in (source.format.LEGACY_DYNAMIC, source.format.HYPOTHESES) and isinstance(
            value, dict
        ):
            lower_legacy_value(source, path, name, value, self.facts)
        data = _JSON.dump_json(value)
        if name == "AcceptedReading" and isinstance(value, dict):
            self.facts.accepted_reading(source, path, value)
        if isinstance(value, dict) and name in (
            "LiveFlow",
            "LiveFlows",
            "RecordedOperation",
            "Recorded",
            "OperationResult",
            "ProviderSessionRecord",
        ):
            self._embedded(source, path, name, value)
        if (
            name == "RoundRecord"
            and isinstance(value, dict)
            and ("round_number" in value or "round" in value)
        ):
            # Embedded round bytes have the same permanent boundary as standalone records.
            record = read_round(source, self.registry, selector=path, data=data)
            lower_round(source, path, record, self.facts)
        elif name in _TYPES:
            decoded = decode(source, _TYPES[name], self.registry, selector=path, data=data)
            self.facts.project(source, path, decoded)

    def _embedded(self, source: ImportSource, path: str, name: str, value: dict[str, Any]) -> None:
        if name == "LiveFlow":
            self.read(source, f"{path}.snapshot_json", value["snapshot_json"], value["name"])
        elif name == "LiveFlows" and value.get("published") is not None:
            self.read(source, f"{path}.published", value["published"], "published")
        elif name in ("RecordedOperation", "Recorded"):
            self.read(source, f"{path}.result_json", value["result_json"], "operation-result")
        elif name == "OperationResult" and value.get("outcome_json") is not None:
            if value["outcome_schema"] != value["operation_schema"]["outcome_schema"]:
                raise FactImportError(
                    f"{source.path}:{path}.outcome_schema", "operation outcome schema mismatch"
                )
            schema = decode(
                source,
                TypeAdapter(OperationSchemaRef),
                self.registry,
                selector=f"{path}.operation_schema",
                data=_JSON.dump_json(value["operation_schema"]),
            )
            try:
                result = self.registry.decode_outcome(schema, value["outcome_json"])
            except ValueError as error:
                raise FactImportError(f"{source.path}:{path}.outcome_json", str(error)) from error
            self.facts.project(source, f"{path}.outcome", result)
        elif name == "ProviderSessionRecord":
            self.facts.external(source, path, "provider-session", value["session_id"])
