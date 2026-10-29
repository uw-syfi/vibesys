"""Every product Pydantic model rejects keys it does not declare, unless it says why not.

Pydantic's default (``extra="ignore"``) drops an undeclared key without a word. On data that
crosses a process or trust boundary (config files, wire requests, files read back, agent
replies and tool arguments) a dropped key is a misspelled field whose default then runs
instead, so every model here sets ``extra="forbid"``. Being a contract is not something a
model declares, because a model built only in code today is parsed from a file tomorrow;
the strict setting is the default and an exception is the declared case.

A model that must accept unknown keys (a reader of a newer writer's records, replay of
history written before a field was removed) declares it in its class body, with a reason
this check reads::

    model_config = ConfigDict(extra="ignore")
    unknown_keys_reason: ClassVar[str] = "History written before X was removed still loads."

A reason covers the class that states it and that class's subclasses in the same module,
where its author sees them (a family of event payloads declares it once on its base). A
subclass in any other module inherits the lenient setting but not the reason, so it states
its own or forbids unknown keys.

Pydantic also decodes dataclasses and TypedDicts: as a model's field type, or through a
module-level ``TypeAdapter``. Those default to ignoring unknown keys too, so the same rule
covers every one of them that a product model or adapter reaches (a dataclass sets
``__pydantic_config__ = ConfigDict(extra="forbid")``; a TypedDict sets
``__pydantic_config__`` with ``with_config``).
"""

from __future__ import annotations

import importlib
import sys
from functools import cache
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, RootModel, TypeAdapter, create_model

REPOSITORY = Path(__file__).resolve().parents[2]
REASON = "unknown_keys_reason"


def _product_modules() -> list[str]:
    """Every importable product module: ``src/`` and ``libs/*/src``, without tests."""
    roots = [REPOSITORY / "src", *sorted((REPOSITORY / "libs").glob("*/src"))]
    names: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            parts = list(path.relative_to(root).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            if parts[-1:] == ["__main__"] or "tests" in parts:
                continue
            if (root / parts[0] / "__init__.py").exists():
                names.append(".".join(parts))
    return names


def _subclasses(model: type[BaseModel]) -> list[type[BaseModel]]:
    found: list[type[BaseModel]] = []
    for sub in model.__subclasses__():
        found.append(sub)
        found.extend(_subclasses(sub))
    return found


def _is_product(module: str) -> bool:
    root = module.partition(".")[0]
    in_product = root.startswith("vs_") or root in {
        "entrypoints",
        "headless",
        "launch",
        "server",
        "vibesys",
    }
    return in_product and ".tests." not in module


@cache
def _product_models() -> list[type[BaseModel]]:
    for name in _product_modules():
        importlib.import_module(name)
    return [
        model
        for model in dict.fromkeys(_subclasses(BaseModel))
        if _is_product(model.__module__)
        and model.__pydantic_generic_metadata__["origin"] is None
        and not issubclass(model, RootModel)
    ]


def _decoded_classes(schema: object) -> dict[type, str | None]:
    """The dataclasses and TypedDicts a core schema decodes, each with its ``extra`` setting."""
    found: dict[type, str | None] = {}
    if isinstance(schema, dict):
        if schema.get("type") in ("dataclass", "typed-dict"):
            config = schema.get("config") or {}
            found[schema["cls"]] = schema.get("extra_behavior") or config.get(
                "extra_fields_behavior"
            )
        for value in schema.values():
            found.update(_decoded_classes(value))
    elif isinstance(schema, list):
        for value in schema:
            found.update(_decoded_classes(value))
    return found


def _adapters() -> list[TypeAdapter[object]]:
    _product_models()
    return [
        value
        for name, module in list(sys.modules.items())
        if _is_product(name)
        for value in list(vars(module).values())
        if isinstance(value, TypeAdapter)
    ]


def _name(model: type) -> str:
    return f"{model.__module__}.{model.__qualname__}"


def _declared_reason(model: type) -> str | None:
    """The reason ``model`` accepts unknown keys, stated on it or on a base in its module."""
    for owner in model.__mro__:
        if REASON in vars(owner):
            reason = vars(owner)[REASON]
            covered = owner is model or owner.__module__ == model.__module__
            return reason if covered and isinstance(reason, str) and reason.strip() else None
    return None


def test_every_model_forbids_unknown_keys_or_declares_why_not() -> None:
    lenient = sorted(
        _name(model)
        for model in _product_models()
        if model.model_config.get("extra") != "forbid" and _declared_reason(model) is None
    )
    assert not lenient, (
        "These models accept unknown keys without saying why. Set "
        "model_config = ConfigDict(extra='forbid'), or declare "
        f"`{REASON}: ClassVar[str]` with the reason they must accept them:\n" + "\n".join(lenient)
    )


def test_a_declared_reason_comes_with_an_explicit_extra_setting() -> None:
    """A reason documents a choice, so the model makes that choice explicitly, not by default."""
    implicit = sorted(
        _name(model)
        for model in _product_models()
        if _declared_reason(model) is not None and "extra" not in model.model_config
    )
    assert not implicit, (
        f"These models declare `{REASON}` but leave `extra` at Pydantic's default; "
        "set it in model_config:\n" + "\n".join(implicit)
    )


def test_every_decoded_dataclass_and_typed_dict_forbids_unknown_keys() -> None:
    schemas = [model.__pydantic_core_schema__ for model in _product_models()]
    schemas.extend(adapter.core_schema for adapter in _adapters())
    decoded: dict[type, str | None] = {}
    for schema in schemas:
        decoded.update(_decoded_classes(schema))
    lenient = sorted(
        _name(cls)
        for cls, extra in decoded.items()
        if _is_product(cls.__module__) and extra != "forbid" and _declared_reason(cls) is None
    )
    assert not lenient, (
        "Pydantic decodes these dataclasses or TypedDicts and drops keys they do not declare; "
        "set __pydantic_config__ = ConfigDict(extra='forbid'):\n" + "\n".join(lenient)
    )


def test_a_reason_does_not_cover_a_subclass_in_another_module() -> None:
    class Lenient(BaseModel):
        model_config = ConfigDict(extra="ignore")
        unknown_keys_reason: ClassVar[str] = "A reader of a newer writer's records."

    class SameModule(Lenient):
        value: int

    elsewhere = create_model("Elsewhere", __base__=Lenient, __module__="elsewhere", value=int)

    assert _declared_reason(Lenient) is not None
    assert _declared_reason(SameModule) == _declared_reason(Lenient)
    assert elsewhere.model_config.get("extra") == "ignore"
    assert _declared_reason(elsewhere) is None
