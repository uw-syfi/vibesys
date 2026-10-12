"""``reply.<field>`` renders the backticked name of a field the schema offers, and only those."""

from __future__ import annotations

import copy
import pickle
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_prompts.api import TemplateRenderer, UnknownReplyFieldError, reply_fields

_NAMES = st.from_regex(r"[a-z][a-z_]{0,10}", fullmatch=True).filter(
    lambda name: not name.startswith("dead")
)


@dataclass(frozen=True)
class _Object:
    """A reply object type: its field names, and the object type each field holds, if any."""

    fields: dict[str, _Object | None] = field(default_factory=dict)


def _objects(depth: int) -> st.SearchStrategy[_Object]:
    if depth == 0:
        return st.builds(_Object, st.dictionaries(_NAMES, st.none(), max_size=4))
    child = st.none() | _objects(depth - 1)
    return st.builds(_Object, st.dictionaries(_NAMES, child, max_size=4))


def _names_of(node: _Object) -> set[str]:
    return set(node.fields) | {
        name for child in node.fields.values() if child is not None for name in _names_of(child)
    }


class _Schema:
    """Renders an object tree as JSON Schema, holding each child in a rotating position."""

    def __init__(self) -> None:
        self.defs: dict[str, object] = {}
        self._count = 0

    def node(self, node: _Object) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {name: self._value(child) for name, child in node.fields.items()},
        }

    def _value(self, child: _Object | None) -> dict[str, object]:
        if child is None:
            return {"type": "string"}
        self._count += 1
        match self._count % 4:
            case 0:
                return self.node(child)
            case 1:
                return {"$ref": self._define(child)}
            case 2:
                return {"type": "array", "items": {"$ref": self._define(child)}}
            case _:
                return {"anyOf": [{"type": "null"}, {"$ref": self._define(child)}]}

    def _define(self, child: _Object) -> str:
        key = f"Def{len(self.defs)}"
        self.defs[key] = {}
        self.defs[key] = self.node(child)
        return f"#/$defs/{key}"


def _schema(root: _Object, *, dead: _Object | None = None) -> dict[str, object]:
    builder = _Schema()
    schema = builder.node(root)
    if dead is not None:
        builder.defs["Dead"] = {
            "type": "object",
            "properties": {f"dead_{name}": {"type": "string"} for name in dead.fields},
        }
    schema["$defs"] = builder.defs
    return schema


@given(root=_objects(2), dead=_objects(0))
def test_a_schema_offers_exactly_the_fields_it_reaches(root: _Object, dead: _Object) -> None:
    fields = reply_fields(_schema(root, dead=dead))

    assert set(fields) == _names_of(root)


@given(root=_objects(2))
def test_an_offered_field_renders_as_its_backticked_name(root: _Object) -> None:
    fields = reply_fields(_schema(root))
    renderer = TemplateRenderer(Path())

    for name in fields:
        assert renderer.render_string("{{ reply." + name + " }}", reply=fields) == f"`{name}`"
        assert renderer.render_string("{{ reply." + name + ".name }}", reply=fields) == name


@given(root=_objects(2), dead=_objects(0).filter(lambda node: bool(node.fields)))
def test_a_field_the_schema_does_not_offer_fails_the_render(root: _Object, dead: _Object) -> None:
    fields = reply_fields(_schema(root, dead=dead))
    renderer = TemplateRenderer(Path())

    for name in dead.fields:
        with pytest.raises(UnknownReplyFieldError):
            renderer.render_string("{{ reply.dead_" + name + " }}", reply=fields)


def test_a_conditional_test_does_not_hide_a_field_the_schema_lacks() -> None:
    fields = reply_fields({"type": "object", "properties": {"kept": {"type": "string"}}})

    with pytest.raises(UnknownReplyFieldError):
        TemplateRenderer(Path()).render_string(
            "{% if reply.gone is defined %}x{% endif %}", reply=fields
        )


def test_a_field_named_like_a_container_method_is_still_a_field() -> None:
    names = ("items", "keys", "names", "get", "from_schema")
    fields = reply_fields({"properties": {name: {"type": "string"} for name in names}})

    assert set(fields) == set(names)
    assert {str(getattr(fields, name)) for name in names} == {f"`{name}`" for name in names}


def test_a_reference_to_a_missing_definition_is_an_error() -> None:
    with pytest.raises(ValueError, match="no definition"):
        reply_fields({"properties": {"a": {"$ref": "#/$defs/Gone"}}, "$defs": {}})


def test_a_reference_outside_the_definitions_is_an_error() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        reply_fields({"properties": {"a": {"$ref": "other.json#/x"}}})


def test_the_error_names_the_field_and_what_is_offered() -> None:
    fields = reply_fields({"properties": {"beta": {}, "alpha": {}}})

    with pytest.raises(UnknownReplyFieldError, match=r"'gamma'.*alpha, beta"):
        _ = fields.gamma


@given(root=_objects(2))
def test_python_protocol_lookups_behave_normally(root: _Object) -> None:
    fields = reply_fields(_schema(root))

    assert not hasattr(fields, "__html__")
    assert getattr(fields, "__anything__", "default") == "default"
    assert pickle.dumps(fields)  # pickling shares the protocol copy relies on
    for clone in (
        copy.copy(fields),
        copy.deepcopy(fields),
    ):
        assert set(clone) == set(fields)
        assert [str(getattr(clone, name)) for name in sorted(clone)] == [
            f"`{name}`" for name in sorted(fields)
        ]


@given(root=_objects(2))
def test_an_ordinary_unknown_name_is_not_an_attribute_error(root: _Object) -> None:
    fields = reply_fields(_schema(root))

    with pytest.raises(UnknownReplyFieldError) as raised:
        _ = fields.not_offered_anywhere
    assert not isinstance(raised.value, AttributeError)
