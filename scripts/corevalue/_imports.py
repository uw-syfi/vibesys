"""Resolve imported value types and conversion helpers by their owning proto file."""

from __future__ import annotations

from protobuf import (
    DescEnum,
    DescFieldValueEnum,
    DescFieldValueList,
    DescFieldValueMap,
    DescFieldValueMessage,
    DescMessage,
)

from scripts.corevalue._schema import name


def foreign_types(file: object) -> tuple[DescMessage | DescEnum, ...]:
    """Collect references once, excluding the built-in Timestamp representation."""
    found = {}
    for message in file.messages:
        for field in message.fields:
            for kind in _elements(field.value):
                if (
                    isinstance(kind, DescMessage | DescEnum)
                    and kind.file.name != file.name
                    and kind.type_name != "google.protobuf.Timestamp"
                ):
                    found[kind.type_name] = kind
    return tuple(found[key] for key in sorted(found))


def imports(file: object, package: str, *, boundary: bool) -> list[str]:
    """Import each type or converter from its defining file, with deterministic ordering."""
    grouped = {}
    for kind in foreign_types(file):
        module = kind.file.name.removesuffix(".proto").replace("/", ".")
        suffix = "_boundary" if boundary else "_cv"
        symbols = (f"encode_{name(kind)}", f"decode_{name(kind)}") if boundary else (name(kind),)
        grouped.setdefault(f"{package}.{module}{suffix}", set()).update(symbols)
    return [
        f"from {module} import " + ", ".join(sorted(symbols))
        for module, symbols in sorted(grouped.items())
    ]


def _elements(value: object) -> tuple[object, ...]:
    match value:
        case DescFieldValueMessage(message=message):
            return (message,)
        case DescFieldValueEnum(enum=enum):
            return (enum,)
        case DescFieldValueList(element=element):
            return (element,)
        case DescFieldValueMap(value=element):
            return (element,)
        case _:
            return ()
