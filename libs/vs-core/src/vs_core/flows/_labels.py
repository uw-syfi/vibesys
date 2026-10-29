"""Span labels: the kinds a search declares at ``@search`` and the labels its flows apply.

A ``SpanKind`` names what a span means to the search (a workstream, a gate), with the schema of
its attributes, the verdicts it may reach and the relations it may link by. ``ctx.label`` labels
the current task's span; ``label=Label(...)`` on an aspect call labels the operation's span when
it is issued. ``labeled`` checks a label against the declarations and returns the span's new
``SpanLabel``; anything undeclared is a ``ContractError`` naming the key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

from pydantic import BaseModel, ValidationError

from vs_core._registry import ContractError

from ._spans import SpanLabel, SpanLink

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from ._spans import SpanId


@dataclass(frozen=True)
class SpanKind:
    """A kind of span the search labels: its attributes, verdicts and link relations.

    ``attrs`` is a pydantic model that forbids unknown keys (None: the kind has no attributes).
    """

    name: str
    attrs: type[BaseModel] | None = None
    verdicts: tuple[str, ...] = ()
    links: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Reject a kind whose declaration could not reject undeclared keys."""
        if not self.name:
            raise ContractError(("spans",), "a span kind needs a name")
        if self.attrs is not None and self.attrs.model_config.get("extra") != "forbid":
            message = f"the attributes of {self.name!r} must forbid unknown keys (extra='forbid')"
            raise ContractError(("spans", self.name, "attrs"), message)
        for field, names in (("verdicts", self.verdicts), ("links", self.links)):
            if len(set(names)) != len(names) or not all(names):
                message = f"{field} of {self.name!r} must be distinct, non-empty names"
                raise ContractError(("spans", self.name, field), message)


class LabelFields(TypedDict, total=False):
    """The fields a label sets; a field ``ctx.label`` does not name keeps its value."""

    title: str | None
    links: tuple[SpanLink, ...]
    attrs: BaseModel | Mapping[str, object] | None
    verdict: str | None
    reason: str | None


_FIELDS = frozenset(LabelFields.__annotations__)


@dataclass(frozen=True)
class Label:
    """A whole label, for ``label=`` on an aspect call: the operation's span gets it when issued."""

    kind: SpanKind
    title: str | None = None
    links: tuple[SpanLink, ...] = ()
    attrs: BaseModel | Mapping[str, object] | None = None
    verdict: str | None = None
    reason: str | None = None

    def fields(self) -> LabelFields:
        """The label's fields, every one named."""
        return LabelFields(
            title=self.title,
            links=self.links,
            attrs=self.attrs,
            verdict=self.verdict,
            reason=self.reason,
        )


def declared(kinds: tuple[SpanKind, ...]) -> Mapping[str, SpanKind]:
    """The search's span kinds by name; two kinds with one name are a ContractError."""
    names = [kind.name for kind in kinds]
    if len(set(names)) != len(names):
        raise ContractError(("spans",), f"span kinds must have distinct names: {sorted(names)}")
    return {kind.name: kind for kind in kinds}


def labeled(
    kinds: Mapping[str, SpanKind],
    current: SpanLabel | None,
    kind: SpanKind,
    fields: LabelFields,
    exists: Callable[[SpanId], bool],
) -> SpanLabel:
    """The span's label after ``fields`` of ``kind`` replace those they name in ``current``."""
    if kinds.get(kind.name) is not kind:
        message = f"span kind {kind.name!r} is not declared by the search (@search spans=)"
        raise ContractError(("label", "kind"), message)
    if current is not None and current.kind != kind.name:
        message = f"the span is a {current.kind!r}; a span keeps the kind of its first label"
        raise ContractError(("label", "kind"), message)
    unknown = sorted(set(fields) - _FIELDS)
    if unknown:
        raise ContractError(("label", unknown[0]), f"a label has no field {unknown[0]!r}")
    update: dict[str, object] = {
        key: value for key, value in fields.items() if key in {"title", "reason"}
    }
    if "verdict" in fields:
        update["verdict"] = _verdict(kind, fields["verdict"])
    if "links" in fields:
        update["links"] = tuple(_link(kind, link, exists) for link in fields["links"])
    if "attrs" in fields:
        update["attrs_json"] = _attrs_json(kind, fields["attrs"])
    return (current or SpanLabel(kind=kind.name)).model_copy(update=update)


def _verdict(kind: SpanKind, verdict: str | None) -> str | None:
    if verdict is not None and verdict not in kind.verdicts:
        message = f"{verdict!r} is not a verdict of {kind.name!r}: {kind.verdicts}"
        raise ContractError(("label", kind.name, "verdict"), message)
    return verdict


def _link(kind: SpanKind, link: SpanLink, exists: Callable[[SpanId], bool]) -> SpanLink:
    if link.rel not in kind.links:
        message = f"{link.rel!r} is not a link relation of {kind.name!r}: {kind.links}"
        raise ContractError(("label", kind.name, "links", link.rel), message)
    if not exists(link.to):
        message = f"the link {link.rel!r} names no span of this run: {link.to}"
        raise ContractError(("label", kind.name, "links", link.to), message)
    return link


def _attrs_json(kind: SpanKind, attrs: BaseModel | Mapping[str, object] | None) -> str | None:
    if attrs is None:
        return None
    if kind.attrs is None:
        raise ContractError(("label", kind.name, "attrs"), f"{kind.name!r} declares no attributes")
    if isinstance(attrs, BaseModel):
        if type(attrs) is not kind.attrs:
            message = f"the attributes of {kind.name!r} are a {kind.attrs.__name__}"
            raise ContractError(("label", kind.name, "attrs"), message)
        return attrs.model_dump_json()
    try:
        model = kind.attrs.model_validate(dict(attrs))
    except ValidationError as invalid:
        error = invalid.errors()[0]
        key = ".".join(str(part) for part in error["loc"])
        message = f"attribute {key!r} of {kind.name!r}: {error['msg']}"
        raise ContractError(("label", kind.name, "attrs", key), message) from invalid
    return model.model_dump_json()
