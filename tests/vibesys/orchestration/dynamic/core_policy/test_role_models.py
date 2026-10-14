"""The model an operator chose for one dynamic role reaches that role's search declaration only.

The host hands the policy the models of `[agent.roles.<id>]` that differ from the run's
(`CoreRunContext.role_models`, keyed by role id) and the plan carries them on the search's roles.
Which config key names which role is `test_role_overrides.py`, and that core gives a turn the
model of its role and no other is `libs/vs-core/tests/test_role_governs_session.py`.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic.core_policy._inputs import context_of, resolved

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.orchestration.dynamic.search.api import ROLES, dynamic
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from pathlib import Path

_IDS = {dynamic.ids.role_id(role.name).root: role.name for role in ROLES}
_MODELS = st.text(alphabet="abcdefghij-", min_size=1, max_size=12)


@settings(
    max_examples=examples(pr=10, nightly=25),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(chosen=st.dictionaries(st.sampled_from(sorted(_IDS)), _MODELS))
def test_a_model_chosen_for_a_role_id_reaches_that_roles_declaration_and_no_other(
    tmp_path: Path, chosen: dict[str, str]
) -> None:
    assert PLUGIN.core is not None
    context = replace(context_of(resolved(tmp_path)), role_models=chosen)

    plan = PLUGIN.core.plan(context)

    assert {role.name: role.model for role in plan.search.roles} == {
        name: chosen.get(role_id) for role_id, name in _IDS.items()
    }
