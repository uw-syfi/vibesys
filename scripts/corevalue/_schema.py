"""Descriptor shapes and validation used by the immutable value compiler."""

from __future__ import annotations

from dataclasses import dataclass

from protobuf import (
    DescEnum,
    DescField,
    DescFieldValueEnum,
    DescFieldValueList,
    DescFieldValueMap,
    DescFieldValueMessage,
    DescFieldValueScalar,
    DescMessage,
    ScalarType,
)

from vs_proto.api import core_value_options as options
from vs_proto.api import validation_rules as rules


class GenerationError(ValueError):
    """A named descriptor or rule cannot be represented without losing guarantees."""

    def __init__(self, path: str, detail: str) -> None:
        super().__init__(f"{path}: {detail}")


@dataclass
class Options:
    """The two output modes share one descriptor compiler."""

    boundary: bool = False
    value_module: str = "vs_core.api"
    pb_module: str = "vs_proto.api.core_state"


def extension(desc: object, key: object, default: object = None) -> object:
    """Extension."""
    opts = desc.proto.options
    return opts[key] if opts is not None and key in opts else default


def name(desc: DescMessage | DescEnum) -> str:
    """Name."""
    return desc.name if desc.parent is None else name(desc.parent) + desc.name


def scalar(kind: ScalarType) -> str:
    """Python scalar type, including the protobuf integer representation's intrinsic bounds."""
    simple = {
        ScalarType.STRING: "str",
        ScalarType.BYTES: "bytes",
        ScalarType.BOOL: "bool",
        ScalarType.DOUBLE: "float",
        ScalarType.FLOAT: "float",
    }
    if kind in simple:
        return simple[kind]
    signed = kind not in (
        ScalarType.UINT32,
        ScalarType.UINT64,
        ScalarType.FIXED32,
        ScalarType.FIXED64,
    )
    bits = (
        32
        if kind
        in (
            ScalarType.INT32,
            ScalarType.SINT32,
            ScalarType.SFIXED32,
            ScalarType.UINT32,
            ScalarType.FIXED32,
        )
        else 64
    )
    minimum = -(2 ** (bits - 1)) if signed else 0
    maximum = 2 ** (bits - (1 if signed else 0)) - 1
    return f"Annotated[int, AfterValidator(partial(bounded_integer, minimum={minimum}, maximum={maximum}))]"


def members(enum: DescEnum) -> list[tuple[str, str, int]]:
    """Members."""
    found = []
    for value in enum.values:
        if value.number == 0 and value.name.endswith("_UNSPECIFIED"):
            continue
        member = value.local_name
        opts = extension(value, options.ext_core_member)
        spelling = opts.spelling if opts and opts.spelling else member.lower()
        found.append((member, spelling, value.number))
    return found


def is_union(message: DescMessage) -> bool:
    """Is union."""
    opts = extension(message, options.ext_core_value)
    return bool(opts and opts.union)


def union_members(message: DescMessage) -> list[DescField]:
    """Union members."""
    oneofs = list(message.oneofs)
    if len(oneofs) != 1 or len(list(message.fields)) != len(list(oneofs[0].fields)):
        raise GenerationError(message.type_name, "union must contain exactly one oneof")
    return list(oneofs[0].fields)


def rule_of(field: DescField) -> object:
    """Rule of."""
    return extension(field, rules.ext_field)


def element_type(kind: DescMessage | DescEnum | ScalarType) -> str:
    """Use the same value representation in fields, repeated fields and maps."""
    if isinstance(kind, DescMessage) and kind.type_name == "google.protobuf.Timestamp":
        return "TimestampValue"
    return name(kind) if isinstance(kind, DescMessage | DescEnum) else scalar(kind)


def type_of(field: DescField) -> str:
    """Type of."""
    match field.value:
        case DescFieldValueScalar(scalar=kind):
            return scalar(kind)
        case DescFieldValueEnum(enum=enum):
            return name(enum)
        case DescFieldValueMessage(message=message):
            return (
                "TimestampValue"
                if message.type_name == "google.protobuf.Timestamp"
                else name(message)
            )
        case DescFieldValueList(element=element):
            item = element_type(element)
            return f"tuple[{item}, ...]"
        case DescFieldValueMap(key=key, value=value):
            item = element_type(value)
            return f"FrozenMap[{scalar(key)}, {item}]"
        case _:
            raise GenerationError(field.parent.type_name, f"{field.name}: unsupported type")


def _constraints(value: object) -> list[str]:
    """Project supported scalar bounds without importing a validation runtime into core."""
    found = []
    for bound in (getattr(value, "greater_than", None), getattr(value, "less_than", None)):
        if bound is not None:
            key = {"gte": "ge", "lte": "le"}.get(bound.field, bound.field)
            found.append(f"{key}={bound.value!r}")
    for proto, python in [
        ("min_len", "min_length"),
        ("max_len", "max_length"),
        ("pattern", "pattern"),
    ]:
        if hasattr(value, proto) and value.has_field(proto):
            found.append(f"{python}={getattr(value, proto)!r}")
    if hasattr(value, "finite") and value.finite:
        found.append("allow_inf_nan=False")
    return found


def _scalar_shape(field: DescField, *, explicit: bool) -> tuple[str, str, list[str]]:
    """Scalar defaults and constructor constraints have one source in the descriptor."""
    ann = type_of(field)
    rule = rule_of(field)
    config = extension(field, options.ext_core_field)
    default = ""
    constraints = []
    if config and config.integer:
        patterns = {"^(0|[1-9][0-9]*)$": "ge=0", "^[1-9][0-9]*$": "ge=1"}
        if (
            not rule
            or not rule.type
            or rule.type.field != "string"
            or rule.type.value.pattern not in patterns
        ):
            raise GenerationError(
                field.parent.type_name,
                f"{field.name}: integer requires a canonical nonnegative decimal string rule",
            )
        ann = "int"
        constraints = [patterns[rule.type.value.pattern]]
    elif rule and rule.type:
        value = rule.type.value
        constraints = _constraints(value)
        if rule.type.field == "string" and value.has_field("const"):
            ann = f"Literal[{value.const!r}]"
            default = repr(value.const)
    if field.proto.proto3_optional:
        return ann + " | None", "None", constraints
    if rule is None and not explicit:
        default = {"str": "''", "bytes": "b''", "bool": "False", "float": "0.0"}.get(ann, "0")
    return ann, default, constraints


def field_shape(field: DescField) -> tuple[str, str, list[str]]:
    """Annotation, default expression and scalar constraints, from proto rules only."""
    ann = type_of(field)
    rule = rule_of(field)
    config = extension(field, options.ext_core_field)
    explicit = bool(config and config.explicit)
    match field.value:
        case DescFieldValueMessage():
            required = rule and rule.required
            return (ann, "", []) if required else (ann + " | None", "None", [])
        case DescFieldValueList():
            return ann, "" if explicit else "()", []
        case DescFieldValueMap():
            return ann, "" if explicit else "FrozenMap()", []
        case DescFieldValueEnum():
            required = rule and rule.type and 0 in rule.type.value.not_in
            return (ann, "", []) if required else (ann + " | None", "None", [])
        case _:
            return _scalar_shape(field, explicit=explicit)
