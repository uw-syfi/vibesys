"""Hypothesis property tests over every distinct reply schema in the catalog.

Two properties, checked for every reply type ``vibesys.roles.ALL_ROLES``
declares (deduplicated by class -- several roles share one reply type, e.g.
every designer role replies with ``OrchestratorPlan``):

1. JSON round-trip preserves values: ``Model.model_validate_json(instance.
   model_dump_json()) == instance`` for arbitrary valid instances, not just
   the minimal one ``test_role_contracts.py`` builds.
2. A role's correction-check (``role.check``), when declared, accepts every
   valid reply -- a ``check`` that could reject its own schema's valid
   output would put ``ctx.agents.turn``'s correction loop in a state no
   reply can ever satisfy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from tests.vibesys.roles._context_strategies import reply_strategy

from vibesys import roles

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.runtime import Role

# Dedupe by reply class: several roles share one reply type and there is no
# reason to run the same property twice for the same schema.
_REPLY_TYPES = sorted({role.reply for role in roles.ALL_ROLES}, key=lambda cls: cls.__qualname__)
_REPLY_TYPE_PARAMS = [pytest.param(cls, id=cls.__qualname__) for cls in _REPLY_TYPES]

_ROLES_WITH_CHECK = [role for role in roles.ALL_ROLES if role.check is not None]
_CHECK_PARAMS = [pytest.param(role, id=f"{role.id}:{role.template}") for role in _ROLES_WITH_CHECK]


@pytest.mark.parametrize("reply_cls", _REPLY_TYPE_PARAMS)
def test_json_round_trip_preserves_values(reply_cls: type[BaseModel]) -> None:
    strategy = reply_strategy(reply_cls)

    @given(instance=strategy)
    @settings(max_examples=50, deadline=None)
    def check(instance: BaseModel) -> None:
        restored = reply_cls.model_validate_json(instance.model_dump_json())
        assert restored == instance

    check()


@pytest.mark.parametrize("role", _CHECK_PARAMS)
def test_correction_check_accepts_every_valid_reply(role: Role) -> None:
    """No role declares ``check`` today; this still runs -- with zero cases,
    not as a silent no-op -- the moment one does, since it enumerates
    ``roles.ALL_ROLES`` rather than naming a role by hand.
    """
    assert role.check is not None
    check_fn = role.check
    strategy = reply_strategy(role.reply)

    @given(reply=strategy)
    @settings(max_examples=25, deadline=None)
    def check(reply: BaseModel) -> None:
        error = check_fn(reply)
        assert error is None, f"{role.id}: check rejected a schema-valid reply: {error}"

    check()
