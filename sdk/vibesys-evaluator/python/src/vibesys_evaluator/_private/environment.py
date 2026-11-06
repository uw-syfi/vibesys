"""What a job's process is told about the job it runs in, as one typed message.

vibesys-cli writes the variables of ``JobEnvironment`` (and ``KubernetesEnvironment`` for a job
that needs a cluster); this module reads them back into the message. The variable a field travels
in is its ``env_name`` option in ``proto/vibesys/cli/v1/env.proto``, so no code here names a
variable, and the rules the message states are the ones checked: a bad value is refused with an
error that names its variable and never repeats the value (some variables hold secrets).

A string field is its value, an unsigned integer is its decimal digits, and a repeated string is
its elements separated by commas. A variable that is unset or empty is the field's zero value.
``encode`` is the inverse; the Go codec in vibesys-cli writes the same bytes, and both are held to
the corpus in ``proto/vibesys/cli/v1/conformance/environment.json``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

import protovalidate
from protobuf.wkt.google.protobuf.descriptor_pb import FieldDescriptorProto

from vibesys_evaluator._proto.vibesys.cli.v1 import env_pb

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from protobuf import DescField, Message

_M = TypeVar("_M", bound="Message")
_Type = FieldDescriptorProto.Type
_Label = FieldDescriptorProto.Label
_MAX_UINT32 = 2**32 - 1


class EnvironmentValueError(ValueError):
    """Values that cannot be what their variables are for.

    ``variables`` names every variable at fault, sorted; the message says why for each and never
    repeats a value.
    """

    def __init__(self, problems: Mapping[str, str]) -> None:
        """Say which variables are wrong (variable to reason)."""
        super().__init__("; ".join(f"{variable}: {reason}" for variable, reason in sorted(problems.items())))
        self.variables = tuple(sorted(problems))

    @property
    def variable(self) -> str:
        """The first variable at fault."""
        return self.variables[0]


def _name(field: DescField) -> str:
    options = field.proto.options
    name = options[env_pb.ext_env_name] if options is not None else ""
    if not name:
        msg = f"{field.parent.type_name}.{field.name} has no env_name"
        raise TypeError(msg)
    return name


def _parse(field: DescField, text: str) -> object:
    kind, label = field.proto.type, field.proto.label
    if label == _Label.REPEATED and kind == _Type.STRING:
        return text.split(",")
    if kind == _Type.STRING:
        return text
    if kind == _Type.UINT32:
        if not text.isascii() or not text.isdigit() or int(text) > _MAX_UINT32:
            msg = "not an unsigned 32-bit number"
            raise ValueError(msg)
        return int(text)
    msg = f"{field.parent.type_name}.{field.name}: a variable holds a string, a list of strings or a 32-bit unsigned integer"
    raise TypeError(msg)


def _hold_to_rules(message: Message, names: Mapping[str, str]) -> None:
    try:
        protovalidate.validate(message)
    except protovalidate.ValidationError as error:
        problems: dict[str, str] = {}
        for violation in error.violations:
            path = violation.proto.field
            elements = path.elements if path is not None else []
            variable = names.get(elements[0].field_name, "") if elements else type(message).__name__
            problems[variable] = violation.proto.message
        raise EnvironmentValueError(problems) from error


def decode(message_type: type[_M], lookup: Callable[[str], str | None]) -> _M:
    """Read ``message_type`` from the variables ``lookup`` answers, then hold it to its rules.

    Raises:
        EnvironmentValueError: Values are malformed or break a rule; ``variables`` names them all.
    """
    message = message_type()
    names: dict[str, str] = {}
    problems: dict[str, str] = {}
    for field in message_type.desc().fields:
        variable = _name(field)
        names[field.name] = variable
        text = lookup(variable)
        if not text:
            continue
        try:
            setattr(message, field.local_name, _parse(field, text))
        except ValueError as error:
            problems[variable] = str(error)
    if problems:
        raise EnvironmentValueError(problems)
    _hold_to_rules(message, names)
    return message


def decode_environ(message_type: type[_M], environ: Mapping[str, str]) -> _M:
    """``decode`` over a mapping, such as ``os.environ``."""
    return decode(message_type, environ.get)


def encode(message: Message) -> dict[str, str]:
    """The variables that carry ``message``: the set fields only, after holding it to its rules."""
    fields = type(message).desc().fields
    _hold_to_rules(message, {f.name: _name(f) for f in fields})
    out: dict[str, str] = {}
    for field in fields:
        value = getattr(message, field.local_name)
        if not value:
            continue
        if field.proto.label == _Label.REPEATED:
            for element in value:
                if "," in element:
                    raise EnvironmentValueError({_name(field): "an element holds a comma"})
            out[_name(field)] = ",".join(value)
        else:
            out[_name(field)] = str(value)
    return out


def name_of(message_type: type[Message], field: str) -> str:
    """The variable ``field`` of ``message_type`` travels in, for an error that tells a person which to set."""
    for candidate in message_type.desc().fields:
        if candidate.name == field:
            return _name(candidate)
    msg = f"{message_type.__name__} has no field {field}"
    raise TypeError(msg)


def job_environment(environ: Mapping[str, str]) -> env_pb.JobEnvironment:
    """What the job is told about itself, from ``environ``."""
    return decode_environ(env_pb.JobEnvironment, environ)


def kubernetes_environment(environ: Mapping[str, str]) -> env_pb.KubernetesEnvironment:
    """The cluster a job that needs one is given, from ``environ``."""
    return decode_environ(env_pb.KubernetesEnvironment, environ)
