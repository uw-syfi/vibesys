"""Emit the shell's protobuf conversion functions separately from pure core values."""

from __future__ import annotations

from protobuf import (
    DescEnum,
    DescField,
    DescFieldValueEnum,
    DescFieldValueList,
    DescFieldValueMap,
    DescFieldValueMessage,
    DescFieldValueScalar,
    DescFile,
    DescMessage,
    ScalarType,
)

from scripts.corevalue._imports import imports as converter_imports
from scripts.corevalue._schema import (
    Options,
    enum_subset,
    extension,
    is_null,
    is_union,
    members,
    name,
    options,
    rule_of,
    type_of,
    union_members,
)


def boundary_file(file: DescFile, settings: Options | None = None) -> str:
    """Converters live outside core; protobuf and protovalidate never enter core's imports."""
    settings = settings or Options()
    pb_module = settings.pb_module.format(file=file.name.rsplit("/", 1)[-1].removesuffix(".proto"))
    lines = [
        f"# Generated from {file.name} by protoc-gen-corevalue. DO NOT EDIT.",
        "from __future__ import annotations",
        "from protobuf import Message, Oneof",
        "from typing import Callable, Literal, cast",
        f"import {settings.value_module} as values",
        f"from {pb_module.rsplit('.', 1)[0]} import {pb_module.rsplit('.', 1)[1]} as pb",
        "from vs_runtime._corevalue_wkt import encode_timestamp, decode_timestamp",
        "",
    ]
    lines.extend(converter_imports(file, settings.boundary_package, boundary=True))
    for enum in file.enums:
        lines.extend(_enum(enum))
    lines.extend(
        [
            "def _required[T](value: T | None) -> T:",
            "    if value is None:",
            "        raise ValueError('Required protobuf message is absent')",
            "    return value",
            "",
        ]
    )
    messages = list(file.messages)
    for message in messages:
        lines.extend(_message(message))
    lines.append("ENCODERS: dict[type, Callable[..., Message]] = {")
    lines.extend(
        f"    values.{name(m)}: encode_{name(m)},"
        for m in messages
        if not is_union(m) and not is_null(m)
    )
    lines.extend(["}", "DECODERS: dict[type, Callable[..., object]] = {"])
    lines.extend(f"    pb.{name(m)}: decode_{name(m)}," for m in messages)
    lines.append("}")
    return "\n".join(lines) + "\n"


def enc_element(kind: object, attr: str) -> str:
    """Enc element."""
    if isinstance(kind, DescMessage):
        return (
            f"encode_timestamp({attr})"
            if kind.type_name == "google.protobuf.Timestamp"
            else f"encode_{name(kind)}({attr})"
        )
    if isinstance(kind, DescEnum):
        return f"encode_{name(kind)}({attr})"
    return attr


def dec_element(kind: object, attr: str) -> str:
    """Dec element."""
    if isinstance(kind, DescMessage):
        return (
            f"decode_timestamp({attr})"
            if kind.type_name == "google.protobuf.Timestamp"
            else f"decode_{name(kind)}({attr})"
        )
    if isinstance(kind, DescEnum):
        return f"decode_{name(kind)}({attr})"
    return attr


def field_expr(f: DescField, *, encode: bool) -> str:
    """Field expr."""
    attr = f"{'v' if encode else 'm'}.{f.local_name}"
    convert = enc_element if encode else dec_element
    decimal = _decimal(f, attr, encode=encode)
    if decimal is not None:
        return decimal
    match f.value:
        case DescFieldValueMessage(message=message):
            rule = rule_of(f)
            required = rule and rule.required
            expression = convert(message, f"_required({attr})" if required and not encode else attr)
            return expression if required else f"None if {attr} is None else {expression}"
        case DescFieldValueEnum(enum=enum):
            expr = convert(enum, attr)
            rule = rule_of(f)
            required = (
                rule and rule.type and (0 in rule.type.value.not_in or bool(rule.type.value.in_))
            )
            subset = enum_subset(f)
            if not encode and subset is not None:
                opts = extension(enum, options.ext_core_enum)
                selected = [
                    repr(spelling) if opts and opts.literal else f"values.{name(enum)}.{member}"
                    for member, spelling, _ in subset
                ]
                expr = f"cast(Literal[{', '.join(selected)}], {expr})"
            return expr if encode or required else f"None if {attr} == 0 else {expr}"
        case DescFieldValueList(element=kind):
            return _sequence_expr(f, kind, attr, encode=encode)
        case DescFieldValueMap(key=_, value=kind):
            expr = convert(kind, f"{attr}[k]")
            content = f"{{k: {expr} for k in sorted({attr})}}"
            return content if encode else f"values.FrozenMap({content})"
        case _:
            rule = rule_of(f)
            result = (
                f"{attr} if m.has_field({f.local_name!r}) else None"
                if not encode and f.proto.proto3_optional
                else attr
            )
            if (
                not encode
                and rule
                and rule.type
                and rule.type.field == "string"
                and rule.type.value.has_field("const")
            ):
                result = f"cast(Literal[{rule.type.value.const!r}], {attr})"
    return result


def _message(message: DescMessage) -> list[str]:
    """Generate both conversions of one message or closed union."""
    lines = []
    n = name(message)
    if is_null(message):
        return [
            f"def encode_{n}(v: None) -> pb.{n}:",
            f"    return pb.{n}()",
            "",
            f"def decode_{n}(m: pb.{n}) -> None:",
            "    return None",
            "",
        ]
    annotation = (
        " | ".join(_union_type(f) for f in union_members(message))
        if is_union(message)
        else f"values.{n}"
    )
    lines.append(f"def encode_{n}(v: {annotation}) -> pb.{n}:")
    if is_union(message):
        union = next(iter(message.oneofs)).local_name
        for f in union_members(message):
            lines.extend(
                [
                    f"    if type(v) is {_union_class(f)}:",
                    f"        return pb.{n}({union}=Oneof({f.local_name!r}, {_union_expr(f, 'v', encode=True)}))",
                ]
            )
        lines.append(f"    raise TypeError('Invalid {n} member: ' + type(v).__name__)")
    else:
        args = ", ".join(f"{f.local_name}={field_expr(f, encode=True)}" for f in message.fields)
        lines.append(f"    return pb.{n}({args})")
    lines.extend(["", f"def decode_{n}(m: pb.{n}) -> {annotation}:"])
    if is_union(message):
        union = next(iter(message.oneofs)).local_name
        lines.append(f"    match m.{union}:")
        for f in union_members(message):
            lines.extend(
                [
                    f"        case Oneof({f.local_name!r}, value):",
                    f"            return {_union_expr(f, 'value', encode=False)}",
                ]
            )
        lines.extend(["        case _:", f"            raise ValueError('Missing {n} member')"])
    else:
        args = ", ".join(f"{f.local_name}={field_expr(f, encode=False)}" for f in message.fields)
        lines.append(f"    return values.{n}({args})")
    lines.append("")
    return lines


def _decimal(f: DescField, attr: str, *, encode: bool) -> str | None:
    """Canonical decimal strings preserve unbounded Python counters."""
    config = extension(f, options.ext_core_field)
    if config and config.integer:
        expr = f"{'str' if encode else 'int'}({attr})"
        if f.proto.proto3_optional:
            return (
                f"None if {attr} is None else {expr}"
                if encode
                else f"{expr} if m.has_field({f.local_name!r}) else None"
            )
        return expr
    return None


def _enum(enum: DescEnum) -> list[str]:
    """Share enum conversions with importing contract files."""
    n = name(enum)
    entries = members(enum)
    encoded = (
        "{" + ", ".join(f"{spelling!r}: pb.{n}.{member}" for member, spelling, _ in entries) + "}"
    )
    decoded = repr({number: spelling for _, spelling, number in entries})
    opts = extension(enum, options.ext_core_enum)
    result = (
        f"cast(Literal[{', '.join(repr(v) for _, v, _ in entries)}], {decoded}[v])"
        if opts and opts.literal
        else f"values.{n}({decoded}[v])"
    )
    annotation = (
        f"Literal[{', '.join(repr(v) for _, v, _ in entries)}]"
        if opts and opts.literal
        else f"values.{n}"
    )
    return [
        f"def encode_{n}(v: {annotation} | None) -> pb.{n}:",
        f"    return {encoded}.get(v, pb.{n}.UNSPECIFIED)",
        "",
        f"def decode_{n}(v: pb.{n}) -> {annotation}:",
        f"    return {result}",
        "",
    ]


def _union_type(field: DescField) -> str:
    """A union's nominal member or exact primitive domain."""
    if isinstance(field.value, DescFieldValueMessage):
        return "None" if is_null(field.value.message) else f"values.{type_of(field)}"
    config = extension(field, options.ext_core_field)
    if config and config.integer:
        return "int"
    if isinstance(field.value, DescFieldValueScalar):
        return {
            ScalarType.STRING: "str",
            ScalarType.BYTES: "bytes",
            ScalarType.BOOL: "bool",
            ScalarType.FLOAT: "float",
            ScalarType.DOUBLE: "float",
        }.get(field.value.scalar, "int")
    raise TypeError(field.name)


def _union_class(field: DescField) -> str:
    """None has a value domain, but its runtime type is NoneType."""
    value = _union_type(field)
    return "type(None)" if value == "None" else value


def _union_expr(field: DescField, value: str, *, encode: bool) -> str:
    """Convert scalar members without inventing a Python wrapper record."""
    config = extension(field, options.ext_core_field)
    if config and config.integer:
        return f"{'str' if encode else 'int'}({value})"
    if isinstance(field.value, DescFieldValueMessage):
        return f"{'encode' if encode else 'decode'}_{name(field.value.message)}({value})"
    return value


def _sequence_expr(field: DescField, kind: object, attr: str, *, encode: bool) -> str:
    """Sets sort before encoding and regain their immutable collection on decode."""
    config = extension(field, options.ext_core_field)
    as_set = bool(config and config.frozen_set)
    collection = f"sorted({attr})" if encode and as_set else attr
    expression = (enc_element if encode else dec_element)(kind, "x")
    if encode:
        return f"[{expression} for x in {collection}]"
    return f"{'frozenset' if as_set else 'tuple'}({expression} for x in {collection})"
