"""A decision field of an agent's reply names its inputs, and its prompt carries them.

Asking an agent to decide from facts its prompt does not show is the failure class behind
#3047: a plan bar over a base nobody supplied, a verdict over evidence nobody rendered. A reply
model marks each decision field with ``DecidedFrom`` (``vibesys.hypothesis``), naming the
fields of the role's prompt context the decision needs.

Three checks hold the declaration:

- every reply model of every registered search is found through its roles, and each field of
  it that makes a choice (a flag, a number, a closed set, or a collection of records that hold
  one) declares inputs, unless it is a measurement the agent only reports (``_REPORTED``), so a
  new reply or a new decision field fails until it declares;
- every declared input is a field of the prompt context the role is asked with;
- each prompt of a role, rendered the way the render operation renders it with a context that
  fills every field with a distinctive value, contains the value of every input its reply
  declares.

A role whose reply declares inputs needs a case (``decision_input_cases``): the context to
render and what each input looks like in the prompt.
"""

from __future__ import annotations

import enum
from types import NoneType, UnionType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Literal,
    TypeAliasType,
    Union,
    get_args,
    get_origin,
)

import pytest
from pydantic import BaseModel
from tests.architecture.decision_input_cases import (
    Case,
    cases,
    orchestration_folder,
    registered_folders,
    renderer,
)
from tests.support.prompt_context import registered_searches

from vibesys.hypothesis import declared_inputs
from vibesys.orchestration.issue_queue.models import IssuePerfEvalResult
from vibesys.orchestration.multi.models import ImplementerResponse
from vibesys.orchestration.profilers import ProfilerSummary
from vibesys.orchestration.single.models import SingleAgentRoundResponse

if TYPE_CHECKING:
    from pathlib import Path

    from vs_core.api.search import Role

_COPIED = "a number the agent copies from a measurement it ran; nothing is decided from facts"
_REPORTED: dict[tuple[type[BaseModel], str], str] = {
    (ProfilerSummary, "perf_metric"): _COPIED,
    (ProfilerSummary, "metrics"): _COPIED,
    (ImplementerResponse, "perf_metric"): _COPIED,
    (ImplementerResponse, "metrics"): _COPIED,
    (ImplementerResponse, "candidate_metrics"): _COPIED,
    (SingleAgentRoundResponse, "perf_metric"): _COPIED,
    (SingleAgentRoundResponse, "candidate_metrics"): _COPIED,
    (IssuePerfEvalResult, "metrics"): _COPIED,
}
_CASES = cases()
_ROLES = {
    f"{search.name}/{role.name}": role for search in registered_searches() for role in search.roles
}


def _model_of(annotation: object) -> type[BaseModel] | None:
    """The one model class an annotation names, looking through ``| None`` and ``Annotated``."""
    if get_origin(annotation) is Annotated:
        return _model_of(get_args(annotation)[0])
    if get_origin(annotation) in (Union, UnionType):
        models = [model for arg in get_args(annotation) if (model := _model_of(arg))]
        return models[0] if len(models) == 1 else None
    return (
        annotation if isinstance(annotation, type) and issubclass(annotation, BaseModel) else None
    )


def _resolves(context: type[BaseModel], path: str) -> bool:
    """Whether the dotted ``path`` names a field of ``context``, through nested models."""
    head, _, rest = path.partition(".")
    field = context.model_fields.get(head)
    if field is None:
        return False
    if not rest:
        return True
    nested = _model_of(field.annotation)
    return nested is not None and _resolves(nested, rest)


def _is_a_choice(annotation: object, seen: frozenset[object] = frozenset()) -> bool:
    """Whether a field of this type picks a flag, a number or a member of a closed set.

    A collection of records, or a record, counts when anything inside it makes a choice.
    """
    if annotation in seen:
        return False
    if isinstance(annotation, TypeAliasType):
        return _is_a_choice(annotation.__value__, seen | {annotation})
    origin = get_origin(annotation)
    if origin is Literal:
        return True
    if origin is not None:
        return any(_is_a_choice(arg, seen) for arg in get_args(annotation) if arg is not NoneType)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return any(
            _is_a_choice(field.annotation, seen | {annotation})
            for field in annotation.model_fields.values()
        )
    return isinstance(annotation, type) and issubclass(annotation, bool | int | float | enum.Enum)


def _decision_fields(model: type[BaseModel]) -> list[str]:
    return [name for name, field in model.model_fields.items() if _is_a_choice(field.annotation)]


def test_every_registered_search_is_one_of_the_built_in_orchestrations() -> None:
    assert {
        orchestration_folder(search) for search in registered_searches()
    } == registered_folders()


@pytest.mark.parametrize("name", _ROLES)
def test_every_decision_field_of_a_reply_declares_its_inputs(name: str) -> None:
    model = _ROLES[name].reply
    declared = declared_inputs(model)
    missing = [
        field
        for field in _decision_fields(model)
        if field not in declared and (model, field) not in _REPORTED
    ]

    assert not missing, f"{model.__name__} decision fields declare no inputs: {missing}"


def test_a_reported_field_is_still_a_decision_field_of_a_reply() -> None:
    replies = {role.reply for role in _ROLES.values()}
    stale = [
        (model.__name__, field)
        for model, field in _REPORTED
        if model not in replies or field not in _decision_fields(model)
    ]

    assert not stale, f"reported fields that no reply decides: {stale}"


def _role_of(case: Case) -> Role:
    return _ROLES[f"{case.search}/{case.role}"]


@pytest.mark.parametrize("name", _CASES)
def test_a_declared_input_is_a_field_of_the_prompt_context(name: str) -> None:
    case = _CASES[name]
    unknown = {
        field: path
        for field, paths in declared_inputs(_role_of(case).reply).items()
        for path in paths
        if not _resolves(type(case.context), path)
    }

    assert not unknown, f"inputs that are no field of {type(case.context).__name__}: {unknown}"


@pytest.mark.parametrize("name", _CASES)
def test_the_rendered_prompt_carries_every_declared_input(name: str, tmp_path: Path) -> None:
    case = _CASES[name]
    search = next(item for item in registered_searches() if item.name == case.search)
    text = renderer(search).render(tmp_path, case.context, subject=case.role, held=case.held)
    absent = {
        field: path
        for field, paths in declared_inputs(_role_of(case).reply).items()
        for path in paths
        if case.needles[path] not in text
    }

    assert not absent, f"the {name} prompt does not show declared inputs {absent}"


@pytest.mark.parametrize("name", _CASES)
def test_every_declared_input_has_a_probe(name: str) -> None:
    case = _CASES[name]
    declared = {path for paths in declared_inputs(_role_of(case).reply).values() for path in paths}

    assert declared <= case.needles.keys()


@pytest.mark.parametrize("name", _CASES)
def test_the_case_asks_the_role_whose_reply_it_checks(name: str) -> None:
    case = _CASES[name]

    assert type(case.context).reply_schema == _role_of(case).reply_schema


def test_every_reply_that_declares_inputs_has_a_case() -> None:
    covered = {f"{case.search}/{case.role}" for case in _CASES.values()}
    uncovered = [name for name, role in _ROLES.items() if declared_inputs(role.reply)]

    assert set(uncovered) <= covered, (
        f"roles with declared inputs and no case: {set(uncovered) - covered}"
    )
