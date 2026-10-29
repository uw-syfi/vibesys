"""A role is one declaration: it validates itself, derives what core needs, and is frozen.

The properties here are about the value alone (no search runs). What a role then governs in a
running session is in ``test_role_governs_session``.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

import vs_core.api as core
from vs_core.api.search import search
from vs_core.api.testing import FAKE_OPERATIONS

MODELS = ("model-a", "model-b", "claude-x")


class Plan(core.Value):
    """A plain reply."""

    n: int


class Change(core.RevisionReply):
    """A reply that may leave a revision to keep."""

    n: int


class Idle(Change):
    """A change reply that says it changed nothing."""

    def produces_revision(self) -> bool:
        """This reply claims no work."""
        return False


EXCHANGES = core.Exchanges(correction="c.j2", resume="r.j2", wait_unrecorded="w.j2")

BASE: dict[str, Any] = {
    "name": "worker",
    "title": "Worker",
    "prompt": "worker.j2",
    "reply": Plan,
    "workspace": None,
    "access": core.Access.READ_ONLY,
    "turn_time": 60.0,
    "corrections": 1,
    "lost_turns": 1,
    "charge": core.Charge.TURN,
    "exchanges": EXCHANGES,
}


def declare(**changes: object) -> core.Role:
    """The base role with ``changes`` applied through validation."""
    return core.Role.model_validate({**BASE, **changes})


NAMES = st.from_regex(r"[a-z][a-z0-9_-]{0,12}", fullmatch=True)
INTS = st.integers(min_value=0, max_value=core.MAX_CORRECTIONS)
TIMES = st.floats(min_value=0.001, max_value=core.MAX_TURN_SECONDS, allow_nan=False)


# -- what is accepted ------------------------------------------------------------------------------


@given(
    fields=st.fixed_dictionaries(
        {
            "name": NAMES,
            "corrections": INTS,
            "lost_turns": INTS,
            "turn_time": TIMES,
            "may_wait_for_evaluations": st.booleans(),
            "model": st.none() | st.sampled_from(MODELS),
        }
    )
)
def test_every_declaration_within_the_bounds_is_accepted_as_written(
    fields: dict[str, Any],
) -> None:
    role = declare(**fields)

    assert {key: getattr(role, key) for key in fields} == fields


def test_the_defaults_are_the_ones_written_on_the_role() -> None:
    role = declare()
    assert role.may_wait_for_evaluations is False
    assert role.correctable == frozenset(core.Correctable)
    assert role.lost_turn_backoff == core.Backoff(first=5.0, cap=120.0)
    assert role.render_time == 300.0
    assert role.model is None


# -- what is rejected ------------------------------------------------------------------------------


@given(
    key=st.text(alphabet="abcdefghijklmnop_", min_size=1, max_size=12).filter(
        lambda key: key not in core.Role.model_fields
    )
)
def test_an_unknown_key_is_rejected_and_named(key: str) -> None:
    with pytest.raises(ValidationError, match=key):
        declare(**{key: 1})


@pytest.mark.parametrize("missing", ["name", "prompt", "reply", "workspace", "access", "turn_time"])
def test_a_required_field_is_required(missing: str) -> None:
    without = {key: value for key, value in BASE.items() if key != missing}
    with pytest.raises(ValidationError, match=missing):
        core.Role.model_validate(without)


@pytest.mark.parametrize("missing", ["corrections", "lost_turns", "charge"])
def test_how_a_role_is_carried_is_never_left_to_a_default(missing: str) -> None:
    without = {key: value for key, value in BASE.items() if key != missing}
    with pytest.raises(ValidationError, match=missing):
        core.Role.model_validate(without)


@given(
    field=st.sampled_from(["corrections", "lost_turns"]),
    value=st.integers(min_value=core.MAX_LOST_TURNS + 1, max_value=10_000) | st.integers(-1000, -1),
)
def test_a_count_beyond_the_hard_cap_or_below_zero_is_rejected(field: str, value: int) -> None:
    with pytest.raises(ValidationError, match=field):
        declare(**{field: value})


@pytest.mark.parametrize(
    ("field", "cap"),
    [("turn_time", core.MAX_TURN_SECONDS), ("render_time", core.MAX_RENDER_SECONDS)],
)
@given(data=st.data())
def test_a_duration_that_is_not_positive_finite_and_under_the_cap_is_rejected(
    field: str, cap: float, data: st.DataObject
) -> None:
    value = data.draw(
        st.floats(allow_nan=True, allow_infinity=True).filter(lambda value: not (0 < value <= cap))
    )
    with pytest.raises(ValidationError, match=field):
        declare(**{field: value})


@given(over=st.floats(min_value=1.0, max_value=1e6))
def test_a_turn_longer_than_the_hard_cap_is_rejected(over: float) -> None:
    with pytest.raises(ValidationError, match="turn_time"):
        declare(turn_time=core.MAX_TURN_SECONDS + over)


@pytest.mark.parametrize("name", ["", "Worker", "1st", "has space", "a:b", "über"])
def test_a_name_that_is_not_an_identifier_of_ids_is_rejected(name: str) -> None:
    with pytest.raises(ValidationError, match="name"):
        declare(name=name)


def test_a_role_is_frozen() -> None:
    role = declare()
    with pytest.raises(ValidationError, match="frozen"):
        role.corrections = 3  # ty: ignore[invalid-assignment]


# -- what must agree: an exchange a role can meet has its template -------------------------------


@given(
    corrections=st.integers(1, core.MAX_CORRECTIONS),
    waits=st.booleans(),
    drop=st.sampled_from(["correction", "resume", "wait_unrecorded"]),
)
def test_a_missing_exchange_template_is_named_when_the_role_can_meet_it(
    *, corrections: int, waits: bool, drop: str
) -> None:
    exchanges = EXCHANGES.model_copy(update={drop: None})
    needed = {
        "correction": True,
        "resume": waits,
        "wait_unrecorded": waits,
    }[drop]
    if needed:
        with pytest.raises(ValidationError, match=f"exchanges.{drop}"):
            declare(corrections=corrections, may_wait_for_evaluations=waits, exchanges=exchanges)
    else:
        declare(corrections=corrections, may_wait_for_evaluations=waits, exchanges=exchanges)


def test_a_role_that_is_never_corrected_and_never_waits_needs_no_exchange() -> None:
    declare(corrections=0, exchanges=core.Exchanges())


def test_a_correction_that_core_never_makes_needs_no_template() -> None:
    declare(corrections=3, correctable=frozenset(), exchanges=core.Exchanges())


def test_a_reply_that_is_already_a_result_or_wait_is_rejected() -> None:
    wire = declare(may_wait_for_evaluations=True).wire_reply
    with pytest.raises(ValidationError, match="reply"):
        declare(reply=wire)


@given(access=st.sampled_from([core.Access.READ_ONLY, core.Access.WRITE_ARTIFACTS]))
def test_a_revision_reply_needs_a_workspace_and_write_candidate_access(access: core.Access) -> None:
    with pytest.raises(ValidationError, match="revision reply"):
        declare(reply=Change, access=access, workspace=core.WorkspaceMode.ISOLATED_CHILD)
    with pytest.raises(ValidationError, match="revision reply"):
        declare(reply=Change, access=core.Access.WRITE_CANDIDATE, workspace=None)
    declare(
        reply=Change,
        access=core.Access.WRITE_CANDIDATE,
        workspace=core.WorkspaceMode.ISOLATED_CHILD,
    )


# -- what is derived -----------------------------------------------------------------------------


@given(waits=st.booleans())
def test_the_wire_reply_is_a_result_or_a_wait_exactly_for_a_role_that_may_wait(
    *, waits: bool
) -> None:
    role = declare(may_wait_for_evaluations=waits)
    assert (role.wire_reply is not Plan) is waits
    assert issubclass(role.wire_reply, core.WaitableReply) is waits
    assert role.wire_reply.model_validate_json('{"n": 1}') is not None


@given(first=NAMES, second=NAMES)
def test_the_reply_schema_is_derived_from_the_name_alone(first: str, second: str) -> None:
    one, other = declare(name=first), declare(name=second, corrections=0, lost_turns=0)
    assert (one.reply_schema == other.reply_schema) is (first == second)
    assert one.reply_schema == declare(name=first, turn_time=9.0, model="m").reply_schema


def test_only_a_revision_reply_that_claims_work_waits_for_the_checkpoint() -> None:
    role = declare(
        reply=Change,
        access=core.Access.WRITE_CANDIDATE,
        workspace=core.WorkspaceMode.ISOLATED_CHILD,
    )
    assert role.reply_needs_checkpoint(Change(n=1))
    assert not role.reply_needs_checkpoint(Idle(n=1))
    assert not declare().reply_needs_checkpoint(Plan(n=1))


# -- the operator's model is the one per-run setting -----------------------------------------------


async def _root(ctx: core.Flow, config: core.Value) -> None:
    del ctx, config


class Config(core.Value):
    """No settings."""


@given(
    chosen=st.dictionaries(st.sampled_from(["a", "b", "c"]), st.sampled_from(MODELS)),
)
def test_a_chosen_model_changes_that_role_and_nothing_else(chosen: dict[str, str]) -> None:
    roles = tuple(declare(name=name) for name in ("a", "b", "c"))
    definition = search("s", config=Config, operations=FAKE_OPERATIONS, roles=roles)(_root)
    changed = definition.with_models(chosen)
    for before, after in zip(definition.roles, changed.roles, strict=True):
        assert after.model == chosen.get(before.name)
        assert after.model_copy(update={"model": None}) == before
    assert changed.reply_schemas == definition.reply_schemas
    assert changed.with_models({}).roles == changed.roles


@given(unknown=st.lists(NAMES.filter(lambda name: name not in "abc"), min_size=1, unique=True))
def test_a_model_for_a_role_the_search_does_not_declare_is_rejected_and_named(
    unknown: list[str],
) -> None:
    roles = (declare(name="a"),)
    definition = search("s", config=Config, operations=FAKE_OPERATIONS, roles=roles)(_root)
    with pytest.raises(core.UnknownRoleError) as raised:
        definition.with_models(dict.fromkeys(unknown, "m") | {"a": "m"})
    assert all(name in str(raised.value) for name in unknown)


def test_a_model_must_be_a_non_empty_string() -> None:
    with pytest.raises(ValidationError, match="model"):
        declare(model="")


# -- what the search says about its roles ------------------------------------------------------------


def test_two_roles_of_one_search_cannot_share_a_name() -> None:
    with pytest.raises(core.ContractError, match="distinct"):
        search("s", config=Config, roles=(declare(), declare(prompt="other.j2")))


def test_the_reply_schemas_of_a_search_are_those_of_its_declared_roles() -> None:
    plain, waiting = declare(name="plain"), declare(name="waits", may_wait_for_evaluations=True)
    definition = search("s", config=Config, roles=(plain, waiting))(_root)
    assert definition.reply_schemas == {
        plain.reply_schema: plain.wire_reply,
        waiting.reply_schema: waiting.wire_reply,
    }


@given(
    st.from_regex(r"[a-z][a-z0-9]*([-_][a-z0-9]+)+", fullmatch=True)
    | st.sampled_from(
        ("judge", "claude", "perf-evaluator", "Perf_evaluator", "", " Judge", "Judge ")
    )
)
def test_a_title_that_is_an_id_or_not_sentence_case_is_refused(title: str) -> None:
    with pytest.raises(ValidationError, match="title"):
        declare(title=title)


@given(st.from_regex(r"[A-Z][A-Za-z0-9]{0,8}( [a-z0-9][A-Za-z0-9]{0,8}){0,3}", fullmatch=True))
def test_a_sentence_case_title_is_kept(title: str) -> None:
    assert declare(title=title).title == title
