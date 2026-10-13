"""A persisted field names the class of every value it holds, or says it serializes by instance.

`encode_json` remembers one JSON text per value instance. Pydantic writes a field by the schema
of its *declared* type, so an instance of a subclass that adds fields, held in a field declared
as its base class, is written without them, and the same instance under a field declared as the
subclass is written with them. One remembered text per instance is therefore right only when
no field can hold a subclass that differs from its declared class, unless the field is
`SerializeAsAny` (the instance's own schema, wherever it sits), the class is `@final`, or the class
rebuilds subclass instances as itself on validation (`revalidate_instances`).

This walks every model reachable from the persisted envelope (core state and the registered
strategy states) and fails on a field that could hold such a subclass. It costs nothing at
runtime: the mechanism is closed here instead of checked on every write.
"""

from __future__ import annotations

import importlib
from types import UnionType
from typing import Annotated, Literal, TypeAliasType, TypeVar, Union, get_args, get_origin

from pydantic import BaseModel

from vs_core.api import CoreState, RunEnvelope, StrategyState

STRATEGY_STATE_MODULES = (
    "vibesys.orchestration.dynamic.strategy._state",
    "vibesys.orchestration.evolve.strategy._state",
    "vibesys.orchestration.issue_queue.strategy._state",
    "vibesys.orchestration.multi.strategy._state",
    "vibesys.orchestration.single.strategy._state",
)


def _product(model: type[BaseModel]) -> bool:
    """Classes of the product, not third-party ones or ones that tests define."""
    root = model.__module__.split(".")[0]
    return (root.startswith("vs_") or root == "vibesys") and ".tests." not in model.__module__


def _subclasses(model: type[BaseModel]) -> list[type[BaseModel]]:
    found: list[type[BaseModel]] = []
    for sub in model.__subclasses__():
        if sub.__pydantic_generic_metadata__["origin"] is None and _product(sub):
            found.append(sub)
        found.extend(_subclasses(sub))
    return found


def _differs(base: type[BaseModel], sub: type[BaseModel]) -> bool:
    """Whether ``sub`` adds a field to ``base`` or changes one of its fields."""
    if set(sub.model_fields) != set(base.model_fields):
        return True
    return any(
        sub.model_fields[name].annotation != field.annotation
        or sub.model_fields[name].default != field.default
        for name, field in base.model_fields.items()
    )


def _serializes_by_instance(marker: object) -> bool:
    return type(marker).__name__ == "SerializeAsAny"


def _named_models(annotation: object, *, by_instance: bool) -> list[tuple[type[BaseModel], bool]]:
    """Models the annotation names, with whether the field serializes them by instance."""
    if isinstance(annotation, TypeAliasType):
        return _named_models(annotation.__value__, by_instance=by_instance)
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Annotated:
        marked = by_instance or any(_serializes_by_instance(m) for m in arguments[1:])
        return _named_models(arguments[0], by_instance=marked)
    if origin is Literal or isinstance(annotation, TypeVar):
        return []
    if origin in (Union, UnionType, tuple, frozenset, dict, list, set):
        return [
            found
            for child in arguments
            if child is not Ellipsis
            for found in _named_models(child, by_instance=by_instance)
        ]
    cls = origin if isinstance(origin, type) else annotation
    if isinstance(cls, type) and issubclass(cls, BaseModel):
        return [(cls, by_instance)]
    return []


def _holds_exactly(model: type[BaseModel]) -> bool:
    """Whether a field declared as ``model`` can only hold that very class."""
    return (
        getattr(model, "__final__", False)
        or model.model_config.get("revalidate_instances") == "subclass-instances"
    )


def _roots() -> list[type[BaseModel]]:
    for module in STRATEGY_STATE_MODULES:
        importlib.import_module(module)
    return [CoreState, RunEnvelope, StrategyState, *_subclasses(StrategyState)]


def test_no_persisted_field_can_hold_a_subclass_that_it_would_write_differently() -> None:
    problems: list[str] = []
    seen: set[type[BaseModel]] = set()
    pending = _roots()
    while pending:
        model = pending.pop()
        if model in seen:
            continue
        seen.add(model)
        for name, field in model.model_fields.items():
            by_field = any(_serializes_by_instance(m) for m in field.metadata)
            for named, by_instance in _named_models(field.annotation, by_instance=by_field):
                if not _product(named):
                    continue
                pending.append(named)
                pending.extend(_subclasses(named))
                if by_instance or _holds_exactly(named):
                    continue
                problems.extend(
                    f"{model.__module__}.{model.__qualname__}.{name} is declared {named.__qualname__}"
                    f" but subclass {sub.__module__}.{sub.__qualname__} changes its fields; "
                    "declare the field SerializeAsAny[...], mark the class @final, or set revalidate_instances='subclass-instances' on it"
                    for sub in _subclasses(named)
                    if _differs(named, sub)
                )
    assert not problems, "\n".join(problems)
