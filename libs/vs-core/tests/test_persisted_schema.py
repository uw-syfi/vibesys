"""The persisted-shape check accepts exactly the changes every saved document survives.

Shapes are built as real Pydantic models from a generated description (fields of a few kinds,
with bounds, defaults and closed sets), so the check is held to the JSON schema Pydantic writes.
Each case applies generated changes to a nested model. A change every old document survives (a
new field with a default, a closed set or union that gains a member, a looser bound, a renamed
class) must be accepted, and every document of the old model must still validate under the new
one. A change some old document may not survive (a removed, retyped or newly required field, a
changed default, a tighter bound, a closed set that loses a member) must be reported with the
field's path.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Literal

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ConfigDict, Field, create_model

from vs_core.api.testing import (
    parse_schema_snapshot,
    schema_incompatibilities,
    schema_snapshot,
    snapshot_problems,
)
from vs_sim.api.testing import examples

type Kind = Literal["int", "str", "lit", "enum", "opt", "list"]
_NAMES = ("alpha", "beta", "gamma", "delta", "epsilon")
_MEMBERS = ("a", "b", "c", "d", "e")


@dataclass(frozen=True)
class Spec:
    """One field: its kind, whether a document must hold it, its default and its bounds."""

    kind: Kind
    required: bool
    hi: int | None = None
    """``le`` of an integer, ``max_length`` of a string or a tuple."""
    lo: int | None = None
    """``ge`` of an integer."""
    members: tuple[str, ...] = ("a",)
    default: object = None


type Model = dict[str, Spec]


def _default(spec: Spec) -> object:
    match spec.kind:
        case "int":
            return 0
        case "str":
            return ""
        case "lit" | "enum":
            return spec.members[0]
        case "opt":
            return None
        case "list":
            return ()


def _specs() -> st.SearchStrategy[Spec]:
    def build(kind: Kind, hi: int | None, lo: int | None, count: int, *, required: bool) -> Spec:
        spec = Spec(
            kind, required, hi=hi, lo=lo if kind == "int" else None, members=_MEMBERS[:count]
        )
        return replace(spec, default=None if required else _default(spec))

    return st.builds(
        build,
        st.sampled_from(("int", "str", "lit", "enum", "opt", "list")),
        st.sampled_from((None, 3, 8)),
        st.sampled_from((None, -5, 0)),
        st.integers(1, 3),
        required=st.booleans(),
    )


def _models() -> st.SearchStrategy[Model]:
    return st.dictionaries(st.sampled_from(_NAMES[:4]), _specs(), min_size=1, max_size=4)


def _annotation(name: str, spec: Spec) -> object:
    match spec.kind:
        case "int":
            return int
        case "str":
            return str
        case "lit":
            return Literal.__getitem__(spec.members)
        case "enum":
            return StrEnum(f"{name.title()}Kind", {m: m for m in spec.members})
        case "opt":
            return int | None
        case "list":
            return tuple[int, ...]


def _model(fields: Model, *, name: str = "Inner") -> type[BaseModel]:
    definitions: dict[str, Any] = {}
    for field, spec in fields.items():
        bounded = spec.kind == "int"
        field_info = Field(
            ... if spec.required else spec.default,
            le=spec.hi if bounded else None,
            ge=spec.lo if bounded else None,
            max_length=spec.hi if spec.kind in {"str", "list"} else None,
        )
        definitions[field] = (_annotation(field, spec), field_info)
    config = ConfigDict(extra="forbid", frozen=True)
    return create_model(name, __config__=config, **definitions)


def _root(inner: type[BaseModel]) -> type[BaseModel]:
    config = ConfigDict(extra="forbid", frozen=True)
    return create_model("Root", __config__=config, inner=(inner, ...), tag=(int, 0))


def _schema(fields: Model, *, name: str = "Inner") -> dict[str, object]:
    return _root(_model(fields, name=name)).model_json_schema()


def _value(spec: Spec) -> st.SearchStrategy[object]:
    match spec.kind:
        case "int":
            return st.integers(
                -5 if spec.lo is None else spec.lo, 9 if spec.hi is None else spec.hi
            )
        case "str":
            return st.text("xyz", max_size=5 if spec.hi is None else spec.hi)
        case "lit" | "enum":
            return st.sampled_from(spec.members)
        case "opt":
            return st.none() | st.integers(-5, 5)
        case "list":
            return st.lists(st.integers(-5, 5), max_size=4 if spec.hi is None else spec.hi)


def _documents(fields: Model) -> st.SearchStrategy[dict[str, object]]:
    required = {name: _value(spec) for name, spec in fields.items() if spec.required}
    optional = {name: _value(spec) for name, spec in fields.items() if not spec.required}
    inner = st.fixed_dictionaries(required, optional=optional)
    return st.fixed_dictionaries({"inner": inner, "tag": st.integers(0, 3)})


def _compatible(fields: Model, data: st.DataObject) -> tuple[Model, str]:
    """``fields`` with one change every document of it survives, and the class's new name."""
    name = data.draw(st.sampled_from(("Inner", "Renamed")))
    changes = ["add"]
    changes += [
        f"grow:{n}"
        for n, s in fields.items()
        if s.kind in {"lit", "enum"} and len(s.members) < len(_MEMBERS)
    ]
    changes += [f"loosen:{n}" for n, s in fields.items() if s.hi is not None or s.lo is not None]
    changes += [f"optional:{n}" for n, s in fields.items() if s.required]
    changes += [
        f"nullable:{n}" for n, s in fields.items() if s.kind == "int" and s.hi is s.lo is None
    ]
    change, _, field = data.draw(st.sampled_from(changes)).partition(":")
    spec = fields.get(field)
    match change, spec:
        case "add", _:
            added = data.draw(_specs())
            return {**fields, "zeta": replace(added, required=False, default=_default(added))}, name
        case "grow", Spec():
            return {**fields, field: replace(spec, members=_MEMBERS[: len(spec.members) + 1])}, name
        case "loosen", Spec():
            return {
                **fields,
                field: replace(spec, hi=None if spec.hi is None else spec.hi + 2, lo=None),
            }, name
        case "optional", Spec():
            return {**fields, field: replace(spec, required=False, default=_default(spec))}, name
        case _, Spec():
            return {**fields, field: replace(spec, kind="opt")}, name
    raise AssertionError(change)


def _incompatible(fields: Model, data: st.DataObject) -> tuple[Model, str]:
    """``fields`` with one change some document of it does not survive, and the field it names."""
    changes = [f"remove:{n}" for n in fields]
    changes += [f"retype:{n}" for n, s in fields.items() if s.kind in {"int", "str"}]
    changes += ["add-required"]
    changes += [f"require:{n}" for n, s in fields.items() if not s.required]
    changes += [
        f"default:{n}"
        for n, s in fields.items()
        if not s.required and s.kind == "str" and s.hi != 0
    ]
    changes += [f"tighten:{n}" for n, s in fields.items() if s.kind in {"int", "str", "list"}]
    changes += [
        f"shrink:{n}" for n, s in fields.items() if s.kind in {"lit", "enum"} and len(s.members) > 1
    ]
    changes += [f"unnull:{n}" for n, s in fields.items() if s.kind == "opt" and s.required]
    change, _, field = data.draw(st.sampled_from(changes)).partition(":")
    spec = fields.get(field, Spec("int", required=True))
    rest = {n: s for n, s in fields.items() if n != field}
    match change:
        case "remove":
            changed, named = rest, field
        case "retype":
            kind: Kind = "str" if spec.kind == "int" else "int"
            changed, named = (
                {
                    **fields,
                    field: replace(
                        spec,
                        kind=kind,
                        hi=None,
                        lo=None,
                        default=None if spec.required else _default(replace(spec, kind=kind)),
                    ),
                },
                field,
            )
        case "add-required":
            changed, named = {**fields, "zeta": spec}, "zeta"
        case "require":
            changed, named = {**fields, field: replace(spec, required=True, default=None)}, field
        case "default":
            changed, named = {**fields, field: replace(spec, default="x")}, field
        case "tighten":
            changed, named = (
                {**fields, field: replace(spec, hi=2 if spec.hi is None else spec.hi - 1)},
                field,
            )
        case "shrink":
            changed, named = {**fields, field: replace(spec, members=spec.members[:-1])}, field
        case _:
            changed, named = {**fields, field: replace(spec, kind="int")}, field
    return changed, named


_EXAMPLES = settings(max_examples=examples(pr=60, nightly=600), deadline=None)


@_EXAMPLES
@given(fields=_models(), data=st.data())
def test_a_change_every_old_document_survives_is_accepted(
    fields: Model, data: st.DataObject
) -> None:
    changed, name = _compatible(fields, data)
    assert schema_incompatibilities(_schema(fields), _schema(changed, name=name)) == ()
    new = _root(_model(changed, name=name))
    for document in data.draw(st.lists(_documents(fields), min_size=1, max_size=3)):
        new.model_validate(document)


@_EXAMPLES
@given(fields=_models(), data=st.data())
def test_a_change_some_old_document_may_not_survive_is_reported_at_its_field(
    fields: Model, data: st.DataObject
) -> None:
    changed, field = _incompatible(fields, data)
    found = schema_incompatibilities(_schema(fields), _schema(changed))
    assert any(problem.path == f"inner.{field}" for problem in found), found


@_EXAMPLES
@given(fields=_models(), version=st.tuples(st.integers(1, 99), st.integers(0, 9)))
def test_a_snapshot_reads_back_as_the_schema_it_was_written_from(
    fields: Model, version: tuple[int, int]
) -> None:
    schema = _schema(fields)
    parsed = parse_schema_snapshot(schema_snapshot("root", version, schema))
    assert (parsed.shape, parsed.version) == ("root", version)
    assert schema_incompatibilities(schema, parsed.schema) == ()
    assert schema_incompatibilities(parsed.schema, schema) == ()


@_EXAMPLES
@given(fields=_models(), first=_specs(), second=_specs())
def test_two_changes_that_each_add_a_field_merge_as_the_union_of_their_lines(
    fields: Model, first: Spec, second: Spec
) -> None:
    """Two branches adding different fields touch different lines, so Git merges them."""

    def lines(model: Model) -> set[str]:
        return set(schema_snapshot("root", (1,), _schema(model)).splitlines())

    one = {**fields, "zeta": replace(first, required=False, default=_default(first))}
    two = {**fields, "eta": replace(second, required=False, default=_default(second))}
    both = {**one, "eta": two["eta"]}
    base = lines(fields)
    assert lines(both) == (lines(one) | lines(two)) - (base - lines(one)) - (base - lines(two))


@_EXAMPLES
@given(fields=_models(), data=st.data())
def test_a_snapshot_keeping_its_version_must_keep_every_old_document(
    fields: Model, data: st.DataObject
) -> None:
    changed, field = _incompatible(fields, data)
    base = schema_snapshot("record", (4, 1), _schema(fields))
    kept = schema_snapshot("record", (4, 1), _schema(changed))
    bumped = schema_snapshot("record", (5, 1), _schema(changed))
    problems = snapshot_problems(base, kept, "x/record.persisted-schema.txt")
    assert any(f"inner.{field}" in p and "record" in p and "bump" in p for p in problems), problems
    assert snapshot_problems(base, bumped, "x/record.persisted-schema.txt") == ()
    compatible, name = _compatible(fields, data)
    assert (
        snapshot_problems(
            base, schema_snapshot("record", (4, 1), _schema(compatible, name=name)), "x"
        )
        == ()
    )


def test_a_snapshot_that_is_dropped_or_lowers_its_version_is_refused() -> None:
    base = schema_snapshot("record", (4, 1), _schema({"alpha": Spec("int", required=True)}))
    lowered = schema_snapshot("record", (3, 9), _schema({"alpha": Spec("int", required=True)}))
    assert any("deleted" in p for p in snapshot_problems(base, None, "x"))
    assert any("lowered" in p for p in snapshot_problems(base, lowered, "x"))
    assert snapshot_problems(None, base, "x") == ()
