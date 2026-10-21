"""Fail generation when a schema cannot preserve its validation or closed value shape."""

from __future__ import annotations

from protobuf import (
    DescEnum,
    DescFieldValueEnum,
    DescFieldValueList,
    DescFieldValueMap,
    DescFieldValueMessage,
    DescFieldValueScalar,
    DescMessage,
    ScalarType,
)

from scripts.corevalue._constants import constants
from scripts.corevalue._schema import (
    GenerationError,
    extension,
    is_union,
    name,
    rule_of,
    rules,
    union_members,
)

_RESERVED = frozenset(
    {
        "model_fields",
        "model_copy",
        "model_dump",
        "model_dump_json",
        "model_validate",
        "model_validate_json",
        "cached_hash",
        "_hash",
    }
)
_SCALAR_RULES = {
    "string": {"const", "min_len", "max_len", "pattern"},
    "bytes": {"min_len", "max_len"},
    "enum": {"defined_only", "not_in"},
}
_NUMERIC_RULES = {"gt", "gte", "lt", "lte", "finite"}


def validate_file(file: object) -> None:
    """Reject unsupported shapes by descriptor name, instead of emitting incomplete code."""
    claimed = set()
    for key, _ in constants(file):
        if not key.isidentifier() or not key.isupper() or key in claimed:
            raise GenerationError(file.name, f"invalid or duplicate exported constant: {key}")
        claimed.add(key)
    for desc in [*file.enums, *file.messages]:
        generated = name(desc)
        if generated in claimed:
            raise GenerationError(file.name, f"generated name collision: {generated}")
        claimed.add(generated)
    for message in file.messages:
        _message(message)
        checks = extension(message, rules.ext_message)
        if checks and {f.name for f in checks} - {"cel"}:
            raise GenerationError(message.type_name, "unsupported message validation options")


def _message(message: object) -> None:
    nested = [
        m for m in message.nested_messages if not (m.proto.options and m.proto.options.map_entry)
    ]
    if nested or message.nested_enums:
        raise GenerationError(
            message.type_name, "nested declarations must be moved to a top-level message"
        )
    if is_union(message):
        members = union_members(message)
        oneof = next(iter(message.oneofs))
        config = extension(oneof, rules.ext_oneof)
        if not config or not config.required:
            raise GenerationError(message.type_name, "a union wrapper must have a required oneof")
        for field in members:
            if not isinstance(field.value, DescFieldValueMessage):
                raise GenerationError(
                    message.type_name, f"{field.name}: union members must be messages"
                )
    elif any(not all(f.proto.proto3_optional for f in oneof.fields) for oneof in message.oneofs):
        raise GenerationError(message.type_name, "oneofs must use a closed union wrapper")
    for field in message.fields:
        _field(field)


def _field(field: object) -> None:
    path = f"{field.parent.type_name}.{field.name}"
    if field.local_name in _RESERVED:
        raise GenerationError(path, "field collides with a generated value method")
    for kind in _elements(field.value):
        _reference(kind, path, field.parent.file)
    rule = rule_of(field)
    if rule is None:
        return
    extra = {f.name for f in rule} - {"required", rule.type.field if rule.type else ""}
    if extra:
        raise GenerationError(path, f"unsupported field rules: {sorted(extra)}")
    if rule.type:
        value = rule.type.value
        allowed = _SCALAR_RULES.get(rule.type.field, _NUMERIC_RULES)
        unknown = {f.name for f in value} - allowed
        if unknown:
            raise GenerationError(path, f"unsupported {rule.type.field} rules: {sorted(unknown)}")
        if rule.type.field == "enum" and value.not_in not in ([], [0]):
            raise GenerationError(
                path, "enum exclusions other than UNSPECIFIED require a distinct closed enum"
            )


def _elements(value: object) -> tuple[object, ...]:
    match value:
        case DescFieldValueMessage(message=message):
            return (message,)
        case DescFieldValueEnum(enum=enum):
            return (enum,)
        case DescFieldValueScalar(scalar=scalar):
            return (scalar,)
        case DescFieldValueList(element=element):
            return (element,)
        case DescFieldValueMap(key=key, value=element):
            return (key, element)
        case _:
            return ()


def _reference(kind: object, path: str, file: object) -> None:
    if kind is ScalarType.FLOAT:
        raise GenerationError(path, "float32 cannot preserve a Python float; use double")
    if (
        isinstance(kind, DescMessage)
        and kind.file is not file
        and kind.type_name != "google.protobuf.Timestamp"
    ):
        raise GenerationError(
            path, f"imported message {kind.type_name} requires its own value binding"
        )
    if isinstance(kind, DescEnum) and kind.file is not file:
        raise GenerationError(path, "imported enums require their own value binding")
