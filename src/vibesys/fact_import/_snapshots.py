"""Frozen positional snapshot readers, independent of live strategies and their code."""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator
from pydantic import JsonValue, TypeAdapter

from vs_core.api import ChargeReceipt, EvidenceRef, InputRecord, RunFacts
from vs_core.api.values import RevisionRef, Sample
from vs_decode.api import Unreadable, decode_json

from ._rounds import lower_round, read_round
from ._snapshot_data import SCHEMAS
from ._source import FactImportError, decode

if TYPE_CHECKING:
    from vs_core.api import OperationRegistry

    from ._facts import FactSet
    from ._source import ImportSource

_JSON = TypeAdapter(JsonValue)
_SCHEMA = TypeAdapter(dict[str, JsonValue])
_TYPES: dict[str, TypeAdapter[Any]] = {
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
def _reader(strategy: str, name: str) -> Draft202012Validator:
    schema = decode_json(
        SCHEMAS[f"{_STRATEGIES[strategy]}:{name}"],
        _SCHEMA,
        source="retained snapshot descriptor",
        max_bytes=4 * 1024 * 1024,
    )
    if isinstance(schema, Unreadable):
        raise FactImportError("descriptor", schema.details())
    return Draft202012Validator(schema)


class SnapshotDecoder:
    """Validate frozen source shapes, then lower only recognized shared fact nodes."""

    def __init__(self, registry: OperationRegistry, facts: FactSet, strategy: str) -> None:
        self.registry = registry
        self.facts = facts
        self.strategy = strategy

    def read(self, source: ImportSource, selector: str, data: str, name: str | None) -> None:
        if name is None:
            decode(source, TypeAdapter(type(None)), self.registry, selector=selector, data=data)
            return
        try:
            reader = _reader(self.strategy, name)
        except KeyError as error:
            raise FactImportError(
                f"{source.path}:{selector}", f"unknown retained snapshot {self.strategy}/{name}"
            ) from error
        value = decode(source, _JSON, self.registry, selector=selector, data=data)
        error = next(reader.iter_errors(value), None)
        if error is not None:
            path = ".".join(str(part) for part in error.absolute_path)
            raise FactImportError(f"{source.path}:{selector}.{path}", error.message)
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
            name = reference.rsplit("/", 1)[-1]
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
        data = _JSON.dump_json(value)
        if name == "RoundRecord":
            # Embedded round bytes have the same permanent boundary as standalone records.
            record = read_round(source, self.registry, selector=path, data=data)
            lower_round(source, path, record, self.facts)
        elif name in _TYPES:
            decoded = decode(source, _TYPES[name], self.registry, selector=path, data=data)
            self.facts.project(source, path, decoded)
