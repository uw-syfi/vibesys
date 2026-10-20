"""The persisted-shape check holds unions, their discriminators and nested classes.

A discriminated union decodes a document by the value of one property (its discriminator), so a
change of that property, or of the class a tag selects, breaks every saved document of the
union under an unchanged version, even when each class on its own would still take them. An
undiscriminated union decodes into the branch that fits best, so a new branch that also takes
an old document may decode it as another class.

Shapes are real Pydantic models built from a generated description: a root holding a union
(alone or in a tuple) of two to four branch classes, each with a ``kind`` tag and a nested
``child`` class of generated fields. A change the check accepts must keep every old document,
decoded as the same branch; a change some old document may not survive must be reported at the
union or the nested field it touches.
"""

from __future__ import annotations

import json
import operator
from dataclasses import dataclass, replace
from functools import reduce
from typing import Annotated, Any, Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from vs_core.api import ENVELOPE_SCHEMA_VERSION, FlowState, RunEnvelope
from vs_core.api.testing import (
    parse_schema_snapshot,
    schema_incompatibilities,
    schema_snapshot,
    snapshot_problems,
)
from vs_sim.api.testing import examples

type FieldKind = Literal["int", "str", "opt"]
_TAGS = ("a", "b", "c", "d", "e")
_FIELDS = ("alpha", "beta", "gamma", "delta")
_CONFIG = ConfigDict(extra="forbid", frozen=True)
_EXAMPLES = settings(max_examples=examples(pr=60, nightly=600), deadline=None)


@dataclass(frozen=True)
class Child:
    """One field of a branch's nested class: its type and whether a document must hold it."""

    kind: FieldKind
    required: bool


@dataclass(frozen=True)
class Branch:
    """One class of the union: its tag, its nested class's fields and any extra tag fields."""

    tag: str
    child: tuple[tuple[str, Child], ...]
    name: str = ""
    tagged_by: tuple[str, ...] = ()
    """Defaulted ``Literal`` fields holding the tag, besides the required ``kind``."""
    padding: bool = False
    """A defaulted extra field, so the branch takes every document of its unpadded twin."""


@dataclass(frozen=True)
class Shape:
    """The root's union: its branches, the property it is discriminated by, and if in a tuple."""

    branches: tuple[Branch, ...]
    discriminator: str | None
    listed: bool


def _children() -> st.SearchStrategy[tuple[tuple[str, Child], ...]]:
    child = st.builds(Child, st.sampled_from(("int", "str", "opt")), st.booleans())
    fields = st.dictionaries(st.sampled_from(_FIELDS[:3]), child, min_size=1, max_size=3)
    return fields.map(lambda found: tuple(sorted(found.items())))


@st.composite
def _shapes(draw: st.DrawFn) -> Shape:
    tags = draw(st.lists(st.sampled_from(_TAGS[:4]), min_size=2, max_size=4, unique=True))
    branches = tuple(Branch(tag, draw(_children())) for tag in tags)
    discriminator = draw(st.sampled_from((None, "kind")))
    return Shape(branches, discriminator, listed=draw(st.booleans()))


def _annotation(kind: FieldKind) -> object:
    return {"int": int, "str": str, "opt": int | None}[kind]


def _default(kind: FieldKind) -> object:
    return {"int": 0, "str": "", "opt": None}[kind]


def _branch_model(branch: Branch) -> type[BaseModel]:
    name = branch.name or f"Branch{branch.tag.upper()}"
    fields: dict[str, Any] = {
        field: (_annotation(spec.kind), ... if spec.required else _default(spec.kind))
        for field, spec in branch.child
    }
    child = create_model(f"{name}Child", __config__=_CONFIG, **fields)
    tag = Literal.__getitem__((branch.tag,))
    extra: dict[str, Any] = dict.fromkeys(branch.tagged_by, (tag, branch.tag))
    if branch.padding:
        extra["padding"] = (int, 0)
    return create_model(name, __config__=_CONFIG, kind=(tag, ...), child=(child, ...), **extra)


def _root(shape: Shape) -> type[BaseModel]:
    members = tuple(_branch_model(branch) for branch in shape.branches)
    union: Any = reduce(operator.or_, members)
    if shape.discriminator is not None:
        union = Annotated[union, Field(discriminator=shape.discriminator)]
    field = tuple[union, ...] if shape.listed else union
    return create_model("Root", __config__=_CONFIG, u=(field, ...))


def _schema(shape: Shape) -> dict[str, object]:
    return _root(shape).model_json_schema()


def _child_value(kind: FieldKind) -> st.SearchStrategy[object]:
    return {
        "int": st.integers(-5, 5),
        "str": st.text("xyz", max_size=3),
        "opt": st.none() | st.integers(-5, 5),
    }[kind]


@st.composite
def _documents(draw: st.DrawFn, shape: Shape) -> dict[str, object]:
    def member() -> dict[str, object]:
        branch = draw(st.sampled_from(shape.branches))
        child = {
            field: draw(_child_value(spec.kind))
            for field, spec in branch.child
            if spec.required or draw(st.booleans())
        }
        return {"kind": branch.tag, "child": child}

    if shape.listed:
        return {"u": [member() for _ in range(draw(st.integers(1, 3)))]}
    return {"u": member()}


def _tags(document: dict[str, object]) -> list[object]:
    members = document["u"] if isinstance(document["u"], list) else [document["u"]]
    return [_dict(member)["kind"] for member in members]


def _dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _within(found: str, path: str) -> bool:
    """Whether ``found`` is ``path`` or a field under it."""
    return found == path or found.startswith((f"{path}.", f"{path}["))


def _path(shape: Shape) -> str:
    return "u[]" if shape.listed else "u"


def _replace_branch(shape: Shape, index: int, branch: Branch) -> Shape:
    branches = list(shape.branches)
    branches[index] = branch
    return replace(shape, branches=tuple(branches))


def _compatible(shape: Shape, data: st.DataObject) -> Shape:
    """``shape`` with one change every document of it survives, decoded as the same branch."""
    unused = [tag for tag in _TAGS if tag not in {b.tag for b in shape.branches}]
    index = data.draw(st.integers(0, len(shape.branches) - 1))
    branch = shape.branches[index]
    child = dict(branch.child)
    change = data.draw(st.sampled_from(("member", "rename", "reorder", "field", "optional")))
    match change:
        case "member":
            added = Branch(data.draw(st.sampled_from(unused)), data.draw(_children()))
            at = data.draw(st.integers(0, len(shape.branches)))
            branches = (*shape.branches[:at], added, *shape.branches[at:])
            return replace(shape, branches=branches)
        case "rename":
            renamed = tuple(replace(b, name=f"Renamed{b.tag.upper()}") for b in shape.branches)
            return replace(shape, branches=renamed)
        case "reorder":
            return replace(shape, branches=tuple(data.draw(st.permutations(shape.branches))))
        case "field":
            child["delta"] = Child(
                data.draw(st.sampled_from(("int", "str", "opt"))), required=False
            )
        case _:
            child = {field: replace(spec, required=False) for field, spec in child.items()}
    return _replace_branch(shape, index, replace(branch, child=tuple(sorted(child.items()))))


def _incompatible(shape: Shape, data: st.DataObject) -> tuple[Shape, str, str]:
    """``shape`` with one change some old document does not survive, the path and a rule word."""
    path = _path(shape)
    changes = ["drop", "remove-field", "require-new", "toggle"]
    changes += ["switch"] if shape.discriminator else ["shadow"]
    change = data.draw(st.sampled_from(changes))
    index = data.draw(st.integers(0, len(shape.branches) - 1))
    branch = shape.branches[index]
    rest = shape.branches[:index] + shape.branches[index + 1 :]
    word = ""
    match change:
        case "drop":
            changed = replace(shape, branches=rest)
        case "remove-field":
            field, _ = data.draw(st.sampled_from(branch.child))
            child = tuple(item for item in branch.child if item[0] != field)
            changed = _replace_branch(shape, index, replace(branch, child=child))
            if shape.discriminator is not None:  # else the closest other branch may be named
                path, word = f"{path}.child.{field}", "removed"
        case "require-new":
            child = (*branch.child, ("delta", Child("int", required=True)))
            changed = _replace_branch(shape, index, replace(branch, child=child))
        case "toggle":
            toggled = None if shape.discriminator else "kind"
            changed, word = replace(shape, discriminator=toggled), "discriminator"
        case "switch":
            tagged = tuple(replace(b, tagged_by=("tag",)) for b in shape.branches)
            changed, word = replace(shape, branches=tagged, discriminator="tag"), "discriminator"
        case _:
            twin = replace(branch, name=f"Twin{branch.tag.upper()}", padding=True)
            changed, word = replace(shape, branches=(twin, *shape.branches)), "another branch"
    return changed, path, word


@_EXAMPLES
@given(shape=_shapes(), data=st.data())
def test_a_union_change_every_old_document_survives_is_accepted(
    shape: Shape, data: st.DataObject
) -> None:
    changed = _compatible(shape, data)
    assert schema_incompatibilities(_schema(shape), _schema(changed)) == ()
    new = _root(changed)
    for document in data.draw(st.lists(_documents(shape), min_size=1, max_size=3)):
        decoded = new.model_validate_json(json.dumps(document)).model_dump(mode="json")
        assert _tags(decoded) == _tags(document)


@_EXAMPLES
@given(shape=_shapes(), data=st.data())
def test_a_union_change_some_old_document_may_not_survive_is_reported(
    shape: Shape, data: st.DataObject
) -> None:
    changed, path, word = _incompatible(shape, data)
    found = schema_incompatibilities(_schema(shape), _schema(changed))
    assert any(_within(p.path, path) and word in p.rule for p in found), found


@_EXAMPLES
@given(shape=_shapes(), data=st.data())
def test_a_union_change_the_check_accepts_keeps_every_old_document_as_its_branch(
    shape: Shape, data: st.DataObject
) -> None:
    """Soundness: whatever change is drawn, acceptance means old documents decode the same."""
    if data.draw(st.booleans()):
        changed = _compatible(shape, data)
    else:
        changed, _, _ = _incompatible(shape, data)
    if schema_incompatibilities(_schema(shape), _schema(changed)):
        return
    new = _root(changed)
    for document in data.draw(st.lists(_documents(shape), min_size=1, max_size=3)):
        decoded = new.model_validate_json(json.dumps(document))
        assert _tags(decoded.model_dump(mode="json")) == _tags(document)
        member = dict(decoded)["u"]
        members = member if shape.listed else (member,)
        assert not any(name.startswith("Twin") for name in map(_class_name, members))


def _class_name(value: object) -> str:
    return type(value).__name__


@_EXAMPLES
@given(shape=_shapes().filter(lambda s: s.discriminator is not None), data=st.data())
def test_switching_the_discriminator_breaks_old_documents_and_is_reported(
    shape: Shape, data: st.DataObject
) -> None:
    """The review's counterexample: each branch gains a defaulted ``tag``, the union keys on it."""
    tagged = tuple(replace(b, tagged_by=("tag",)) for b in shape.branches)
    changed = replace(shape, branches=tagged, discriminator="tag")
    assert any(
        "discriminator" in p.rule
        for p in schema_incompatibilities(_schema(shape), _schema(changed))
    )
    document = data.draw(_documents(shape))
    with pytest.raises(ValidationError) as raised:
        _root(changed).model_validate_json(json.dumps(document))
    assert "union_tag_not_found" in {item["type"] for item in raised.value.errors()}


@_EXAMPLES
@given(shape=_shapes(), version=st.tuples(st.integers(1, 99), st.integers(0, 9)))
def test_a_union_snapshot_reads_back_with_its_discriminator(
    shape: Shape, version: tuple[int, int]
) -> None:
    schema = _schema(shape)
    parsed = parse_schema_snapshot(schema_snapshot("root", version, schema)).schema
    assert schema_incompatibilities(schema, parsed) == ()
    assert schema_incompatibilities(parsed, schema) == ()
    assert ('"propertyName":"kind"' in json.dumps(parsed, separators=(",", ":"))) == (
        shape.discriminator is not None
    )


def _envelope_snapshot() -> str:
    schema = RunEnvelope[FlowState].model_json_schema()
    return schema_snapshot("RunEnvelope[FlowState]", (ENVELOPE_SCHEMA_VERSION,), schema)


def test_a_changed_discriminator_of_the_real_envelope_is_reported() -> None:
    """Regression: renaming the envelope's first discriminator property was accepted."""
    base = _envelope_snapshot()
    assert '"propertyName":"kind"' in base
    head = base.replace('"propertyName":"kind"', '"propertyName":"variant"', 1)
    problems = snapshot_problems(base, head, "envelope.persisted-schema.txt")
    assert any("discriminator" in problem for problem in problems), problems
    assert snapshot_problems(base, base, "envelope.persisted-schema.txt") == ()
