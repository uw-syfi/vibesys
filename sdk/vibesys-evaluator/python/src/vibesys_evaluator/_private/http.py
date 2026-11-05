"""HTTP steps and requests: what an operation's ``build`` returns, ready for the plan.

``get``/``post``/``put`` build a ``Step`` straight into its ``loadgen.proto``
message, so writing the plan costs one encode per operation. A ``Template``
inserts values captured from an earlier step's response; its encoding is
fixed by where it is used (as is in a path, query, header or form value, as a
JSON string inside a JSON body).
"""

from __future__ import annotations

import json as jsonlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

from protobuf import Oneof

from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_POINTER = re.compile(r"^(/[^/]*)+$")
_MAX_STEPS = 16
_MAX_CAPTURES = 16
_MAX_EXCLUSIVE = 256
_MIN_STATUS, _MAX_STATUS = 100, 599
_JSON_CONTENT_TYPE = "application/json"


class _Ref:
    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Ref) and other.name == self.name

    def __hash__(self) -> int:
        return hash(("ref", self.name))


class Template:
    """A text made of literal parts and values captured from earlier steps.

    Built with ``ref`` and ``+``: ``"Bearer " + ref("token")``.
    """

    __slots__ = ("_parts",)
    _parts: tuple[str | _Ref, ...]

    def __add__(self, other: str | Template) -> Template:
        """This text followed by ``other``."""
        return _template(self._parts + _parts_of(other))

    def __radd__(self, other: str) -> Template:
        """``other`` followed by this text."""
        return _template(_parts_of(other) + self._parts)

    def __repr__(self) -> str:
        """The parts, with refs as ``{name}``."""
        text = "".join(p if isinstance(p, str) else "{" + p.name + "}" for p in self._parts)
        return f"Template({text!r})"


def _template(parts: tuple[str | _Ref, ...]) -> Template:
    merged: list[str | _Ref] = []
    for part in parts:
        if isinstance(part, str) and merged and isinstance(merged[-1], str):
            merged[-1] += part
        elif part != "":
            merged.append(part)
    template = Template.__new__(Template)
    template._parts = tuple(merged)
    return template


def _parts_of(value: str | Template) -> tuple[str | _Ref, ...]:
    if isinstance(value, Template):
        return value._parts
    if isinstance(value, str):
        return (value,)
    msg = f"expected a str or a Template, got {type(value).__name__}"
    raise TypeError(msg)


def ref(name: str) -> Template:
    """The value an earlier step of the same request captured as ``name``.

    Args:
        name: A capture name: lowercase letters, digits and ``_``, starting with a letter.

    Returns:
        A template of that one value.
    """
    if not isinstance(name, str) or not _NAME.match(name):
        msg = f"ref name {name!r} must match {_NAME.pattern}"
        raise ValueError(msg)
    return _template((_Ref(name),))


class Step:
    """One HTTP call of a request, built by ``get``, ``post`` or ``put``."""

    __slots__ = ("_captures", "_message", "_refs")
    _message: pb.Step
    _refs: frozenset[str]
    _captures: frozenset[str]

    def __repr__(self) -> str:
        """The method and path."""
        call = self._message.call
        http = call.value if call is not None else None
        if not isinstance(http, pb.HttpCall) or http.path is None:
            return "Step()"
        return f"Step({http.method.name.removeprefix('HTTP_METHOD_')} {_text(http.path)})"


def _text(template: pb.Template) -> str:
    out: list[str] = []
    for part in template.parts:
        value = part.part.value if part.part is not None else ""
        out.append(value if isinstance(value, str) else "{" + value.name + "}")
    return "".join(out)


class _Encoder:
    """Turns values into template messages and collects the refs they use."""

    __slots__ = ("refs",)

    def __init__(self) -> None:
        self.refs: set[str] = set()

    def template(self, value: str | Template, encoding: pb.RefEncoding, where: str) -> pb.Template:
        if isinstance(value, str):
            return _literal(value)
        if not isinstance(value, Template):
            msg = f"{where}: expected a str or a Template, got {type(value).__name__}"
            raise TypeError(msg)
        parts: list[pb.Template.Part] = []
        for part in value._parts:
            if isinstance(part, str):
                parts.append(pb.Template.Part(part=Oneof("literal", part)))
            else:
                self.refs.add(part.name)
                parts.append(
                    pb.Template.Part(part=Oneof("ref", pb.Ref(name=part.name, encoding=encoding)))
                )
        return pb.Template(parts=parts or [pb.Template.Part(part=Oneof("literal", ""))])

    def params(self, values: Mapping[str, str | Template] | None, where: str) -> list[pb.Param]:
        if values is None:
            return []
        if not isinstance(values, Mapping):
            msg = f"{where}: expected a mapping of names to values"
            raise TypeError(msg)
        out: list[pb.Param] = []
        for name, value in values.items():
            if not isinstance(name, str) or not name:
                msg = f"{where}: names are non-empty strings, got {name!r}"
                raise ValueError(msg)
            out.append(
                pb.Param(
                    name=name, value=self.template(value, pb.RefEncoding.RAW, f"{where} {name!r}")
                )
            )
        return out

    def json_body(self, value: object) -> pb.Template | bytes:
        parts: list[str | _Ref] = []
        _json_parts(value, parts, "json")
        if all(isinstance(p, str) for p in parts):
            return "".join(p for p in parts if isinstance(p, str)).encode()
        merged = _template(tuple(parts))
        return self.template(merged, pb.RefEncoding.JSON_STRING, "json")


_LITERALS: dict[str, pb.Template] = {}
_LITERAL_CACHE_LIMIT = 4096
_LITERAL_CACHED_LENGTH = 64


def _literal(text: str) -> pb.Template:
    # Paths and query values repeat (operation paths, enumerations): share their messages.
    # Shared messages are never mutated after construction.
    cached = _LITERALS.get(text)
    if cached is not None:
        return cached
    message = pb.Template(parts=[pb.Template.Part(part=Oneof("literal", text))])
    if len(text) <= _LITERAL_CACHED_LENGTH and len(_LITERALS) < _LITERAL_CACHE_LIMIT:
        _LITERALS[text] = message
    return message


def _json_parts(value: object, out: list[str | _Ref], where: str) -> None:
    """Append the JSON text of ``value``; a Template becomes a JSON string with its refs inside."""
    if isinstance(value, Template):
        out.append('"')
        out.extend(jsonlib.dumps(p)[1:-1] if isinstance(p, str) else p for p in value._parts)
        out.append('"')
    elif isinstance(value, Mapping):
        out.append("{")
        for n, (key, item) in enumerate(value.items()):
            if not isinstance(key, str):
                msg = f"{where}: object keys are strings, got {key!r}"
                raise TypeError(msg)
            out.append(("," if n else "") + jsonlib.dumps(key) + ":")
            _json_parts(item, out, f"{where}.{key}")
        out.append("}")
    elif isinstance(value, list | tuple):
        out.append("[")
        for n, item in enumerate(value):
            if n:
                out.append(",")
            _json_parts(item, out, f"{where}[{n}]")
        out.append("]")
    else:
        try:
            out.append(jsonlib.dumps(value, allow_nan=False))
        except (TypeError, ValueError) as error:
            msg = f"{where}: {value!r} is not JSON ({error})"
            raise ValueError(msg) from error


def _expect(expect: int | Sequence[int]) -> list[int]:
    statuses = [expect] if isinstance(expect, int) else list(expect)
    if not statuses or len(statuses) > _MAX_STEPS:
        msg = f"expect: 1 to {_MAX_STEPS} statuses, got {statuses!r}"
        raise ValueError(msg)
    for status in statuses:
        if (
            isinstance(status, bool)
            or not isinstance(status, int)
            or not _MIN_STATUS <= status <= _MAX_STATUS
        ):
            msg = f"expect: HTTP statuses are integers in 100..599, got {status!r}"
            raise ValueError(msg)
    return [] if statuses == [200] else statuses  # empty means 200 only


def _captures(capture: Mapping[str, str] | None) -> list[pb.Capture]:
    if capture is None:
        return []
    if len(capture) > _MAX_CAPTURES:
        msg = f"capture: at most {_MAX_CAPTURES} values per step"
        raise ValueError(msg)
    out: list[pb.Capture] = []
    for name, pointer in capture.items():
        if not isinstance(name, str) or not _NAME.match(name):
            msg = f"capture name {name!r} must match {_NAME.pattern}"
            raise ValueError(msg)
        if not isinstance(pointer, str) or not _POINTER.match(pointer):
            msg = f"capture {name!r}: {pointer!r} is not a JSON pointer such as '/token'"
            raise ValueError(msg)
        out.append(pb.Capture(name=name, source=Oneof("json_pointer", pointer)))
    return out


def _step(
    method: pb.HttpMethod,
    path: str | Template,
    *,
    query: Mapping[str, str | Template] | None,
    headers: Mapping[str, str | Template] | None,
    capture: Mapping[str, str] | None,
    expect: int | Sequence[int],
    form: Mapping[str, str | Template] | None = None,
    json: Any = None,
    body: bytes | None = None,
) -> Step:
    encoder = _Encoder()
    if isinstance(path, str) and not path.startswith("/"):
        msg = f"path {path!r} must start with '/'"
        raise ValueError(msg)
    if isinstance(path, Template) and not (
        path._parts and isinstance(path._parts[0], str) and path._parts[0].startswith("/")
    ):
        msg = f"path {path!r} must start with a literal '/'"
        raise ValueError(msg)
    bodies = sum(x is not None for x in (form, json, body))
    if bodies > 1:
        msg = "pass at most one of form, json and body"
        raise ValueError(msg)
    header_params = encoder.params(headers, "headers")
    call = pb.HttpCall(
        method=method,
        path=encoder.template(path, pb.RefEncoding.RAW, "path"),
        query=encoder.params(query, "query"),
        headers=header_params,
    )
    if form is not None:
        call.body = Oneof("form", pb.Form(fields=encoder.params(form, "form")))
    elif json is not None:
        encoded = encoder.json_body(json)
        call.body = Oneof("raw", encoded) if isinstance(encoded, bytes) else Oneof("text", encoded)
        if not any(p.name.lower() == "content-type" for p in header_params):
            header_params.append(pb.Param(name="Content-Type", value=_literal(_JSON_CONTENT_TYPE)))
    elif body is not None:
        if not isinstance(body, bytes | bytearray):
            msg = f"body is bytes, got {type(body).__name__}"
            raise TypeError(msg)
        call.body = Oneof("raw", bytes(body))
    captures = _captures(capture)
    step = Step.__new__(Step)
    step._message = pb.Step(
        call=Oneof("http", call), captures=captures, expect_status=_expect(expect)
    )
    step._refs = frozenset(encoder.refs)
    step._captures = frozenset(c.name for c in captures)
    return step


def get(
    path: str | Template,
    *,
    query: Mapping[str, str | Template] | None = None,
    headers: Mapping[str, str | Template] | None = None,
    capture: Mapping[str, str] | None = None,
    expect: int | Sequence[int] = 200,
) -> Step:
    """A GET of ``path`` below the target.

    Args:
        path: Starts with ``/``.
        query: Query parameters, URL-encoded by loadgen.
        headers: Request headers.
        capture: Values to take from the response by JSON pointer, for later steps.
        expect: The status or statuses that count as success.

    Returns:
        The step.
    """
    return _step(
        pb.HttpMethod.GET, path, query=query, headers=headers, capture=capture, expect=expect
    )


def post(
    path: str | Template,
    *,
    query: Mapping[str, str | Template] | None = None,
    form: Mapping[str, str | Template] | None = None,
    json: Any = None,
    body: bytes | None = None,
    headers: Mapping[str, str | Template] | None = None,
    capture: Mapping[str, str] | None = None,
    expect: int | Sequence[int] = 200,
) -> Step:
    """A POST of ``path`` below the target, with at most one of ``form``, ``json`` and ``body``.

    Args:
        path: Starts with ``/``.
        query: Query parameters, URL-encoded by loadgen.
        form: A URL-encoded form body.
        json: A JSON body (``Content-Type: application/json`` unless headers set one);
            a ``Template`` inside it becomes a JSON string.
        body: A body sent as is.
        headers: Request headers.
        capture: Values to take from the response by JSON pointer, for later steps.
        expect: The status or statuses that count as success.

    Returns:
        The step.
    """
    return _step(
        pb.HttpMethod.POST,
        path,
        query=query,
        form=form,
        json=json,
        body=body,
        headers=headers,
        capture=capture,
        expect=expect,
    )


def put(
    path: str | Template,
    *,
    query: Mapping[str, str | Template] | None = None,
    form: Mapping[str, str | Template] | None = None,
    json: Any = None,
    body: bytes | None = None,
    headers: Mapping[str, str | Template] | None = None,
    capture: Mapping[str, str] | None = None,
    expect: int | Sequence[int] = 200,
) -> Step:
    """A PUT of ``path`` below the target; the arguments are those of ``post``.

    Returns:
        The step.
    """
    return _step(
        pb.HttpMethod.PUT,
        path,
        query=query,
        form=form,
        json=json,
        body=body,
        headers=headers,
        capture=capture,
        expect=expect,
    )


class Request:
    """What one operation sends: steps in order on one client, one latency.

    ``info`` holds what the request was built from; it is not sent, and comes
    back (as JSON) in the ``Call`` that ``check`` and ``apply`` receive.
    Requests with the same ``exclusive`` key never run at the same time, and
    run in plan order.
    """

    __slots__ = ("_exclusive", "_info", "_steps")

    def __init__(
        self, *steps: Step, info: Mapping[str, Any] | None = None, exclusive: str | None = None
    ) -> None:
        """Build the request.

        Args:
            *steps: 1 to 16 steps; a step may ``ref`` only what an earlier one captured.
            info: A JSON object describing the request.
            exclusive: A key (at most 256 characters) no two running requests share.
        """
        if not 1 <= len(steps) <= _MAX_STEPS:
            msg = f"Request takes 1 to {_MAX_STEPS} steps, got {len(steps)}"
            raise ValueError(msg)
        captured: set[str] = set()
        for n, step in enumerate(steps):
            if not isinstance(step, Step):
                msg = f"Request step {n} is a {type(step).__name__}, not a Step (from get, post or put)"
                raise TypeError(msg)
            missing = step._refs - captured
            if missing:
                msg = f"Request step {n} refers to {sorted(missing)} that no earlier step captures"
                raise ValueError(msg)
            captured |= step._captures
        if info is not None and not isinstance(info, Mapping):
            msg = f"Request info is a mapping (a JSON object), got {type(info).__name__}"
            raise TypeError(msg)
        if exclusive is not None and (
            not isinstance(exclusive, str) or not 1 <= len(exclusive) <= _MAX_EXCLUSIVE
        ):
            msg = f"Request exclusive is a string of 1 to {_MAX_EXCLUSIVE} characters, got {exclusive!r}"
            raise ValueError(msg)
        self._steps = steps
        self._info: Mapping[str, Any] = info if info is not None else {}
        self._exclusive = exclusive or ""

    def __repr__(self) -> str:
        """The steps."""
        return f"Request({', '.join(repr(s) for s in self._steps)})"


def planned(request: Request, operation: int, *, keep_bodies: bool) -> pb.PlannedOperation:
    """The plan entry of ``request`` as operation number ``operation``."""
    return pb.PlannedOperation(
        operation=operation,
        steps=[s._message for s in request._steps],
        exclusive_key=request._exclusive,
        keep_bodies=keep_bodies,
    )


def request_info(request: Request) -> Mapping[str, Any]:
    """The request's info, as given."""
    return request._info


def step_message(step: Step) -> pb.Step:
    """The step's plan message (for readiness probes)."""
    return step._message
