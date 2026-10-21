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

from scripts.corevalue._cel import cel
from scripts.corevalue._constants import constants
from scripts.corevalue._imports import foreign_types
from scripts.corevalue._schema import (
    GenerationError,
    enum_subset,
    extension,
    field_shape,
    is_null,
    is_union,
    name,
    options,
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
    "enum": {"defined_only", "not_in", "in"},
    "repeated": {"min_items", "max_items", "items"},
}
_NUMERIC_RULES = {"gt", "gte", "lt", "lte", "finite"}


def validate_file(file: object) -> None:
    """Reject unsupported shapes by descriptor name, instead of emitting incomplete code."""
    claimed = set()
    for key, _ in constants(file):
        if not key.isidentifier() or not key.isupper() or key in claimed:
            raise GenerationError(file.name, f"invalid or duplicate exported constant: {key}")
        claimed.add(key)
    for desc in [*file.enums, *file.messages, *foreign_types(file)]:
        generated = name(desc)
        if generated in claimed:
            raise GenerationError(file.name, f"generated name collision: {generated}")
        claimed.add(generated)
    for message in file.messages:
        _aliases(message, claimed)
        _message(message)
        checks = extension(message, rules.ext_message)
        if checks and {f.name for f in checks} - {"cel"}:
            raise GenerationError(message.type_name, "unsupported message validation options")
        if checks:
            for rule in checks.cel:
                cel(rule.expression, message)


def _message(message: object) -> None:
    if is_null(message) and (message.fields or extension(message, rules.ext_message)):
        raise GenerationError(message.type_name, "null marker must be empty and have no rules")
    nested = [
        m for m in message.nested_messages if not (m.proto.options and m.proto.options.map_entry)
    ]
    if nested or message.nested_enums:
        raise GenerationError(
            message.type_name, "nested declarations must be moved to a top-level message"
        )
    _base_shape(message)
    if is_union(message):
        if extension(message, rules.ext_message):
            raise GenerationError(
                message.type_name, "union wrapper cannot carry message validation"
            )
        _union_domains(message)
        members = union_members(message)
        oneof = next(iter(message.oneofs))
        config = extension(oneof, rules.ext_oneof)
        if not config or not config.required:
            raise GenerationError(message.type_name, "a union wrapper must have a required oneof")
        for field in members:
            if not isinstance(field.value, DescFieldValueMessage | DescFieldValueScalar):
                raise GenerationError(message.type_name, f"{field.name}: unsupported union member")
    elif any(not all(f.proto.proto3_optional for f in oneof.fields) for oneof in message.oneofs):
        raise GenerationError(message.type_name, "oneofs must use a closed union wrapper")
    for field in message.fields:
        _field(field)
        field_shape(field)


def _field(field: object) -> None:
    path = f"{field.parent.type_name}.{field.name}"
    if field.local_name in _RESERVED:
        raise GenerationError(path, "field collides with a generated value method")
    for kind in _elements(field.value):
        _reference(kind, path, field.parent.file)
    config = extension(field, options.ext_core_field)
    if config and config.frozen_set and not isinstance(field.value, DescFieldValueList):
        raise GenerationError(path, "immutable set requires a repeated field")
    if config and config.frozen_set and isinstance(field.value, DescFieldValueList):
        element = field.value.element
        if isinstance(element, DescMessage) or element in (ScalarType.FLOAT, ScalarType.DOUBLE):
            raise GenerationError(
                path, "immutable set requires canonically sortable scalar or enum elements"
            )
    _null_presence(field)
    rule = rule_of(field)
    if rule is None:
        return
    extra = {f.name for f in rule} - {"required", rule.type.field if rule.type else ""}
    if extra:
        raise GenerationError(path, f"unsupported field rules: {sorted(extra)}")
    if rule.type:
        _enum_subset(field, rule.type)
        _item_rule(field, rule.type)
        value = rule.type.value
        allowed = _SCALAR_RULES.get(rule.type.field, _NUMERIC_RULES)
        unknown = {f.name for f in value} - allowed
        if unknown:
            raise GenerationError(path, f"unsupported {rule.type.field} rules: {sorted(unknown)}")


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
    ) and kind.file.proto.package != file.proto.package:
        raise GenerationError(
            path, f"imported message {kind.type_name} requires an explicit value package"
        )
    if (
        isinstance(kind, DescEnum)
        and kind.file is not file
        and kind.file.proto.package != file.proto.package
    ):
        raise GenerationError(path, "imported enum requires an explicit value package")


def _alias(field: object, alias: str, claimed: set[str]) -> None:
    """A published scalar alias comes from exactly one required scalar rule."""
    path = f"{field.parent.type_name}.{field.name}"
    if not alias.isidentifier() or not alias[0].isupper() or alias in claimed:
        raise GenerationError(path, f"invalid or duplicate scalar alias: {alias}")
    if not isinstance(field.value, DescFieldValueScalar) or field.proto.proto3_optional:
        raise GenerationError(path, "scalar alias requires a nonnullable scalar field")
    claimed.add(alias)


def _aliases(message: DescMessage, claimed: set[str]) -> None:
    """Register each scalar alias in the generated file's closed namespace."""
    for field in message.fields:
        config = extension(field, options.ext_core_field)
        if config:
            for alias in config.aliases:
                _alias(field, alias, claimed)


def _item_rule(field: object, typed: object) -> None:
    """Repeated enum rules must agree with the generated closed element domain."""
    if typed.field != "repeated" or typed.value.items is None:
        return
    item = typed.value.items
    if (
        not isinstance(field.value, DescFieldValueList)
        or not isinstance(field.value.element, DescEnum)
        or item.type is None
        or item.type.field != "enum"
        or {f.name for f in item} != {"enum"}
        or {f.name for f in item.type.value} != {"defined_only", "not_in"}
        or not item.type.value.defined_only
        or item.type.value.not_in != [0]
    ):
        raise GenerationError(
            field.parent.type_name, f"{field.name}: unsupported repeated item rules"
        )


def _union_domains(message: DescMessage) -> None:
    """Exact runtime types must identify a unique oneof arm without guessing."""
    config = extension(message, options.ext_core_value)
    domains = set()
    for field in union_members(message):
        domain = _union_domain(field)
        if domain in domains:
            raise GenerationError(message.type_name, "union members have overlapping value domains")
        if config.discriminator and not isinstance(field.value, DescFieldValueMessage):
            raise GenerationError(
                message.type_name, "a discriminated union requires message members"
            )
        domains.add(domain)


def _union_domain(field: object) -> object:
    value = field.value
    if isinstance(value, DescFieldValueMessage):
        if is_union(value.message):
            raise GenerationError(
                field.parent.type_name, "nested union wrappers require flattening"
            )
        return type(None) if is_null(value.message) else value.message.type_name
    config = extension(field, options.ext_core_field)
    if isinstance(value, DescFieldValueScalar):
        if config and config.integer:
            return int
        return {
            ScalarType.STRING: str,
            ScalarType.BYTES: bytes,
            ScalarType.BOOL: bool,
            ScalarType.DOUBLE: float,
            ScalarType.FLOAT: float,
        }.get(value.scalar, int)
    raise GenerationError(field.parent.type_name, f"{field.name}: unsupported union domain")


def _enum_subset(field: object, typed: object) -> None:
    """A selected enum domain must contain only represented nonzero members."""
    if typed.field != "enum":
        return
    enum_subset(field)


def _base_shape(message: DescMessage) -> None:
    """A nominal base is local and every inherited field keeps its original contract."""
    config = extension(message, options.ext_core_value)
    if not config or not config.base:
        return
    base = next((m for m in message.file.messages if m.name == config.base), None)
    if base is None or is_union(base) or is_null(base):
        raise GenerationError(message.type_name, f"unsupported base value: {config.base}")
    positions = {m.name: index for index, m in enumerate(message.file.messages)}
    if config.mixin or positions[base.name] >= positions[message.name]:
        raise GenerationError(
            message.type_name, "base value must precede its child without a second mixin"
        )
    base_checks = extension(base, rules.ext_message)
    child_checks = extension(message, rules.ext_message)
    inherited_rules = {rule.expression for rule in base_checks.cel} if base_checks else set()
    declared_rules = {rule.expression for rule in child_checks.cel} if child_checks else set()
    if inherited_rules - declared_rules:
        raise GenerationError(
            message.type_name, "inherited CEL must also be declared on the child proto"
        )
    fields = {field.name: field for field in message.fields}
    for field in base.fields:
        inherited = fields.get(field.name)
        if inherited is None or field_shape(inherited) != field_shape(field):
            raise GenerationError(
                message.type_name, f"{field.name}: inherited contract differs from base"
            )


def _null_presence(field: object) -> None:
    """None is a scalar-union value, never an ambiguous optional message sentinel."""
    if is_union(field.parent) or not isinstance(field.value, DescFieldValueMessage):
        return
    target = field.value.message
    if is_null(target):
        raise GenerationError(field.parent.type_name, "null markers require a scalar union arm")
    rule = rule_of(field)
    if (
        is_union(target)
        and not (rule and rule.required)
        and any(
            isinstance(member.value, DescFieldValueMessage) and is_null(member.value.message)
            for member in union_members(target)
        )
    ):
        raise GenerationError(field.parent.type_name, "optional null union loses wire presence")
