"""Emit the shell's protobuf conversion functions separately from pure core values."""

from __future__ import annotations

from protobuf import (
    DescEnum,
    DescField,
    DescFieldValueEnum,
    DescFieldValueList,
    DescFieldValueMap,
    DescFieldValueMessage,
    DescFile,
    DescMessage,
)

from scripts.corevalue._schema import (
    Options,
    extension,
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
    lines = [
        f"# Generated from {file.name} by protoc-gen-corevalue. DO NOT EDIT.",
        "from __future__ import annotations",
        "from protobuf import Message, Oneof",
        "from typing import Callable, Literal, cast",
        f"import {settings.value_module} as values",
        f"from {settings.pb_module.rsplit('.', 1)[0]} import {settings.pb_module.rsplit('.', 1)[1]} as pb",
        "from vs_runtime._corevalue_wkt import encode_timestamp, decode_timestamp",
        "",
    ]
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
    lines.extend(f"    values.{name(m)}: encode_{name(m)}," for m in messages if not is_union(m))
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
        table = (
            "{"
            + ", ".join(
                f"{spelling!r}: pb.{name(kind)}.{member}" for member, spelling, _ in members(kind)
            )
            + "}"
        )
        return f"{table}.get({attr}, pb.{name(kind)}.UNSPECIFIED)"
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
        table = {number: spelling for _, spelling, number in members(kind)}
        opts = extension(kind, options.ext_core_enum)
        converted = f"{table!r}[{attr}]"
        return (
            f"cast(values.{name(kind)}, {converted})"
            if opts and opts.literal
            else f"values.{name(kind)}({converted})"
        )
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
            required = rule and rule.type and 0 in rule.type.value.not_in
            return expr if encode or required else f"None if {attr} == 0 else {expr}"
        case DescFieldValueList(element=kind):
            expr = convert(kind, "x")
            return f"{'[' if encode else 'tuple('}{expr} for x in {attr}{']' if encode else ')'}"
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
    annotation = (
        " | ".join(f"values.{type_of(f)}" for f in union_members(message))
        if is_union(message)
        else f"values.{n}"
    )
    lines.append(f"def encode_{n}(v: {annotation}) -> pb.{n}:")
    if is_union(message):
        union = next(iter(message.oneofs)).local_name
        for f in union_members(message):
            lines.extend(
                [
                    f"    if type(v) is values.{type_of(f)}:",
                    f"        return pb.{n}({union}=Oneof({f.local_name!r}, encode_{type_of(f)}(v)))",
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
                    f"            return decode_{type_of(f)}(value)",
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
