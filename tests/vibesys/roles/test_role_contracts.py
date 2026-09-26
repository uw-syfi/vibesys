"""Per-``Role`` contract tests, parametrized over every role in the catalog.

Enumerates ``vibesys.roles.ALL_ROLES`` (never a hardcoded role list, so a
newly added role is covered automatically) and checks, for each:

- its template renders end to end with a real ``vibesys.prompts`` renderer
  (``StrictUndefined``: every free variable the template reads must come
  from the role's own context model) against a hypothesis-generated valid
  instance of ``role.context``;
- ``role.reply`` validates a minimal valid instance;
- ``role.fallback()`` (and ``role.timeout_fallback()``, when declared)
  produce a valid instance of ``role.reply``;
- ``role.access``/``role.session``/``role.paid``/``role.check`` are
  internally coherent.

No monkeypatching or mocks: rendering goes through the real
``vibesys.prompts.render_template``/``Prompt`` (the same machinery
``ctx.agents.turn`` uses), and every reply/fallback call is the real
callable a strategy would invoke.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from jinja2 import UndefinedError
from tests.vibesys.roles._context_strategies import context_strategy, reply_strategy

from vibesys import roles
from vibesys.constants import ComputeBackend
from vibesys.prompts import PROMPTS_DIR, Prompt, render_template
from vibesys.runtime import ReadOnly

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.runtime import Role

_ROLE_PARAMS = [pytest.param(role, id=f"{role.id}:{role.template}") for role in roles.ALL_ROLES]
_SUPPRESS = (HealthCheck.too_slow, HealthCheck.function_scoped_fixture)


def _split_template(template: str) -> tuple[Path, str]:
    """Mirror ``vibesys.orchestration.agents._split_template``."""
    parts = Path(template).parts
    assert len(parts) >= 3, f"{template!r} needs a strategy folder"
    return PROMPTS_DIR / parts[0] / parts[1], "/".join(parts[2:])


def _render(role: Role, context: BaseModel) -> str:
    """Render ``role.template`` exactly as ``ctx.agents.turn`` would.

    Every role renders through plain ``render_template`` except the handful
    (today: issue_queue's system prompts) that ``ctx.agents.turn`` renders
    through the backend-aware ``Prompt`` instead, which auto-injects compute
    fragments (``device_dtype``, etc.) as extra kwargs. Since a ``Role``
    doesn't itself say which path it needs, try plain rendering first and
    fall back to a backend-aware render only on the specific failure mode
    (a fragment name left undefined) that distinguishes the two paths.
    """
    template_dir, name = _split_template(role.template)
    kwargs = context.model_dump(mode="python")
    try:
        return render_template(name, template_dir=template_dir, **kwargs)
    except UndefinedError as plain_error:
        try:
            return Prompt(template_dir, ComputeBackend.CUDA).render(name, **kwargs)
        except UndefinedError:
            raise plain_error from None


@pytest.mark.parametrize("role", _ROLE_PARAMS)
def test_template_renders_with_a_valid_context_instance(role: Role) -> None:
    strategy = context_strategy(role.context)

    @given(context=strategy)
    @settings(max_examples=10, deadline=None, suppress_health_check=_SUPPRESS)
    def check(context: BaseModel) -> None:
        _render(role, context)

    check()


@pytest.mark.parametrize("role", _ROLE_PARAMS)
def test_reply_schema_accepts_a_minimal_valid_instance(role: Role) -> None:
    strategy = reply_strategy(role.reply)

    @given(reply=strategy)
    @settings(max_examples=5, deadline=None, suppress_health_check=_SUPPRESS)
    def check(reply: BaseModel) -> None:
        assert isinstance(reply, role.reply)

    check()


@pytest.mark.parametrize("role", _ROLE_PARAMS)
def test_fallback_produces_a_valid_reply(role: Role) -> None:
    reply = role.fallback()
    assert isinstance(reply, role.reply)
    # Round-trips: a fallback reply is not exempt from the schema it claims.
    assert role.reply.model_validate_json(reply.model_dump_json()) == reply


@pytest.mark.parametrize("role", _ROLE_PARAMS)
def test_timeout_fallback_produces_a_valid_reply_when_declared(role: Role) -> None:
    if role.timeout_fallback is None:
        pytest.skip(f"{role.id}: no distinct timeout_fallback declared")
    reply = role.timeout_fallback(30.0)
    assert isinstance(reply, role.reply)


@pytest.mark.parametrize("role", _ROLE_PARAMS)
def test_timeout_fallback_is_distinguishable_from_parse_failure_fallback(role: Role) -> None:
    """A declared ``timeout_fallback`` exists precisely so a caller reading
    the reply can tell "the agent timed out" from "the agent replied with
    something unparseable" -- if the two fallbacks dumped identically, that
    distinction would be lost.
    """
    if role.timeout_fallback is None:
        pytest.skip(f"{role.id}: no distinct timeout_fallback declared")
    assert role.fallback().model_dump() != role.timeout_fallback(30.0).model_dump()


@pytest.mark.parametrize("role", _ROLE_PARAMS)
def test_read_only_roles_are_never_paid_writers(role: Role) -> None:
    """``before_paid``/the paid marker exists for work a later round or gate
    may need to check out; a ``ReadOnly`` role's edits are reverted, so
    marking one ``paid`` would record billable work that never persists.
    """
    if isinstance(role.access, ReadOnly):
        assert not role.paid, f"{role.id}: ReadOnly role is marked paid"


@pytest.mark.parametrize("role", _ROLE_PARAMS)
def test_correction_budget_and_check_are_declared_together(role: Role) -> None:
    """``max_corrections`` only does something when ``check`` exists to fail
    the reply, and a declared ``check`` with no correction budget can never
    trigger a reprompt -- both directions are configuration bugs.
    """
    has_check = role.check is not None
    has_budget = role.max_corrections > 0
    assert has_check == has_budget, (
        f"{role.id}: check={role.check!r} max_corrections={role.max_corrections} "
        "must be declared together"
    )
