"""What a role declares is what governs its session: stories on a flow that asks one agent.

Nothing here is hard-coded in core: each property varies one field of the role and reads what
the session did. ``FlowRun`` plays the host with scripted outcomes and no clock, and every story
also runs with the state written to JSON and replayed after each step.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.testing import FakeView, FlowRun, Prompt, fake_search
from vs_sim.api.testing import examples

from .flow_support import Config, Plan, open_workspace

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from pydantic import BaseModel

    from vs_core.api.testing import Asked

_SETTINGS = settings(
    max_examples=examples(pr=5, nightly=25), deadline=None, derandomize=True, database=None
)
_RESTARTING = pytest.mark.parametrize("restarting", [False, True], ids=["live", "restart"])

EXCHANGES = core.Exchanges(correction="c.j2", resume="r.j2", wait_unrecorded="w.j2")


class Change(core.RevisionReply):
    """A reply that may leave a revision to keep."""

    n: int


class Idle(Change):
    """A change that says it changed nothing."""

    def produces_revision(self) -> bool:
        """This reply claims no work."""
        return False


class Outcome(core.Value):
    """How the one ask ended."""

    ended: str
    """The type of what the session gave: ``TurnFinished`` or ``Failed``."""
    reason: str | None = None
    refusal: str | None = None
    checkpoint: bool = False


def role_of(**changes: object) -> core.Role:
    """A read-only worker role with these settings changed."""
    fields: dict[str, Any] = {
        "name": "worker",
        "title": "Worker",
        "prompt": "worker.j2",
        "reply": Plan,
        "workspace": None,
        "access": core.Access.READ_ONLY,
        "turn_time": 600.0,
        "corrections": 1,
        "lost_turns": 1,
        "charge": core.Charge.TURN,
        "exchanges": EXCHANGES,
    }
    return core.Role.model_validate({**fields, **changes})


def outcome_of(result: core.TurnFinished | core.Failed) -> Outcome:
    """The record a story reads."""
    if isinstance(result, core.Failed):
        return Outcome(
            ended="Failed",
            reason=result.reason.value,
            refusal=result.refusal.value if result.refusal is not None else None,
        )
    return Outcome(ended="TurnFinished", checkpoint=result.checkpoint is not None)


def start(
    role: core.Role,
    *,
    restarting: bool = False,
    within: float | None = None,
    view: FakeView | None = None,
    definition: Callable[..., Coroutine[Any, Any, None]] | None = None,
) -> FlowRun[Config]:
    """A search that asks ``role`` once and publishes how it ended."""

    async def root(ctx: core.Flow, config: Config) -> None:
        del config
        result = await ctx.agents.session(role).turn(Prompt(text="go"), within=within)
        ctx.publish(outcome_of(result))

    search = fake_search("govern", Config, (role,))(definition or root)
    return FlowRun.start(search, Config(), view=view, restarting=restarting)


def published(run: FlowRun[Config]) -> Outcome | None:
    """How the ask ended, once it did."""
    return run.driver.published(run.state, Outcome)


def turn(run: FlowRun[Config]) -> Asked:
    """The one turn the search has open."""
    turns = [asked for asked in run.open if isinstance(asked.request, core.RequestTurn)]
    assert len(turns) == 1, [asked.decision.root for asked in run.open]
    return turns[0]


def spec_of(asked: Asked) -> core.TurnSpec:
    """The turn request's spec."""
    assert isinstance(asked.request, core.RequestTurn)
    return asked.request.turn


def invocation_of(asked: Asked) -> core.InvocationRef:
    """The invocation the turn request names."""
    assert isinstance(asked.request, core.RequestTurn)
    return core.InvocationRef(
        session_id=spec_of(asked).session.session_id,
        invocation_id=spec_of(asked).invocation_id,
        generation=asked.request.scope.generation,
    )


NOT_JSON = "this is not json"
WAITING = json.dumps({"kind": "waiting_for_evaluation", "handles": ["h"]})
SUSPENDING = FakeView(
    changes={"capabilities": core.Capabilities(lifecycle=frozenset({"suspend"}), operations=())}
)

# -- the turn's time -------------------------------------------------------------------------------

TIMES = st.floats(min_value=0.5, max_value=core.MAX_TURN_SECONDS, allow_nan=False)


@_SETTINGS
@given(turn_time=TIMES, within=st.none() | TIMES)
@_RESTARTING
def test_a_turn_may_take_the_roles_turn_time_or_the_narrower_limit_of_the_ask(
    *, restarting: bool, turn_time: float, within: float | None
) -> None:
    run = start(role_of(turn_time=turn_time), restarting=restarting, within=within)

    expected = turn_time if within is None else min(within, turn_time)
    assert turn(run).until == core.Within(limit=expected)


def test_the_watches_of_an_ask_bound_the_turn_with_the_roles_turn_time() -> None:
    watch = core.WhileAlive(job=core.ResourceId(root="job"))

    async def root(ctx: core.Flow, config: Config) -> None:
        del config
        within = core.Within(limit=9999.0) | watch
        result = await ctx.agents.session(role_of(turn_time=30.0)).turn(
            Prompt(text="go"), within=within
        )
        ctx.publish(outcome_of(result))

    run = start(role_of(turn_time=30.0), definition=root)

    assert turn(run).until == core.Within(limit=30.0, watches=(watch,))


@_SETTINGS
@given(render_time=st.floats(min_value=0.5, max_value=core.MAX_RENDER_SECONDS, allow_nan=False))
def test_a_prompt_may_take_the_roles_render_time_to_render(render_time: float) -> None:
    run = start(role_of(render_time=render_time))

    first = run.decisions[0]
    assert isinstance(first, core.Wait)
    assert isinstance(first.request, core.Operation)
    assert first.until.limit == render_time


# -- corrections ---------------------------------------------------------------------------------------


@_SETTINGS
@given(corrections=st.integers(0, core.MAX_CORRECTIONS), after=st.none() | st.integers(0, 5))
@_RESTARTING
def test_an_invalid_reply_is_corrected_as_often_as_the_role_says_then_reported(
    *, restarting: bool, corrections: int, after: int | None
) -> None:
    role = role_of(corrections=corrections)
    run = start(role, restarting=restarting)
    mistakes = corrections if after is None else min(after, corrections)

    asked = turn(run)
    for _ in range(mistakes):
        run.reply_raw(asked, NOT_JSON)
        follow = turn(run)
        assert follow.template == EXCHANGES.correction
        assert isinstance(follow.context, core.ReplyCorrection)
        assert follow.context.role == "worker"
        assert spec_of(follow).charge_class == "correction"
        assert spec_of(follow).predecessor == invocation_of(asked)
        assert spec_of(follow).session == spec_of(asked).session, (
            "a correction continues the session"
        )
        asked = follow

    if after is not None and after < corrections:
        run.reply(asked, Plan(n=1))
        assert published(run) == Outcome(ended="TurnFinished")
    else:
        run.reply_raw(asked, NOT_JSON)
        assert published(run) == Outcome(ended="Failed", reason="invalid-reply")
        assert not [a for a in run.open if isinstance(a.request, core.RequestTurn)]


def test_a_role_that_is_never_corrected_reports_its_first_invalid_reply() -> None:
    run = start(role_of(corrections=0, exchanges=core.Exchanges()))

    run.reply_raw(turn(run), NOT_JSON)

    assert published(run) == Outcome(ended="Failed", reason="invalid-reply")


# -- which failures are corrected ------------------------------------------------------------------------


@_SETTINGS
@given(
    correctable=st.frozensets(st.sampled_from(list(core.Correctable))),
    may_wait=st.booleans(),
    failure=st.sampled_from(["invalid", "unrecorded"]),
)
def test_a_failed_reply_is_corrected_exactly_when_the_role_lists_it(
    *, correctable: frozenset[core.Correctable], may_wait: bool, failure: str
) -> None:
    role = role_of(correctable=correctable, may_wait_for_evaluations=may_wait, corrections=2)
    run = start(role)

    run.reply_raw(turn(run), NOT_JSON if failure == "invalid" else WAITING)

    # A role that cannot wait sees a waiting reply as simply invalid.
    if failure == "invalid" or not may_wait:
        kind, template, reason = (
            core.Correctable.INVALID_REPLY,
            EXCHANGES.correction,
            "invalid-reply",
        )
    else:
        kind, template, reason = (
            core.Correctable.UNRECORDED_WAIT,
            EXCHANGES.wait_unrecorded,
            "wait-refused",
        )
    if kind in correctable:
        follow = turn(run)
        assert follow.template == template
        assert spec_of(follow).charge_class == "correction"
    else:
        assert published(run) == Outcome(ended="Failed", reason=reason)


def test_an_unrecorded_wait_is_corrected_with_what_the_wait_was() -> None:
    run = start(role_of(may_wait_for_evaluations=True))

    run.reply_raw(turn(run), WAITING)

    follow = turn(run)
    assert follow.context == core.WaitUnrecorded(role="worker")
    assert follow.template == EXCHANGES.wait_unrecorded


# -- lost turns ----------------------------------------------------------------------------------------


@_SETTINGS
@given(
    lost_turns=st.integers(0, core.MAX_LOST_TURNS),
    first=st.floats(min_value=0.5, max_value=50.0, allow_nan=False),
    cap=st.floats(min_value=50.0, max_value=500.0, allow_nan=False),
)
@_RESTARTING
def test_a_lost_turn_is_asked_again_as_often_and_after_as_long_as_the_role_says(
    *, restarting: bool, lost_turns: int, first: float, cap: float
) -> None:
    backoff = core.Backoff(first=first, cap=cap)
    role = role_of(lost_turns=lost_turns, lost_turn_backoff=backoff)
    run = start(role, restarting=restarting)
    now = 0.0

    asked = turn(run)
    for lost in range(lost_turns):
        run.lose(asked)
        assert run.sleeping_until == (now + backoff.delay(lost),)
        now += backoff.delay(lost)
        run.advance_to(now)
        again = turn(run)
        assert again.context == asked.context
        assert again.template == asked.template
        assert spec_of(again).charge_class == "correction"
        assert spec_of(again).predecessor == invocation_of(asked)
        asked = again

    run.lose(asked)
    assert published(run) == Outcome(ended="Failed", reason="lost")
    assert run.sleeping_until == ()


# -- a turn that waited, and was lost when resumed ---------------------------------------------------------


@_SETTINGS
@example(lost_turns=1)  # the dynamic implementer and judge (review of #3283, weak spot 1)
@given(lost_turns=st.integers(0, core.MAX_LOST_TURNS))
@_RESTARTING
def test_a_lost_resume_is_asked_again_once_if_the_role_asks_lost_turns_again(
    *, restarting: bool, lost_turns: int
) -> None:
    """A resumed turn the transport lost is asked again once, as a correction, then reported.

    Before (review of #3283, weak spot 1), it was never asked again, so an outage that began
    in a resumed turn's last minutes, too late for the capacity gate to hold it, lost the
    whole workstream.
    """
    backoff = core.Backoff(first=5.0, cap=120.0)
    role = role_of(may_wait_for_evaluations=True, lost_turns=lost_turns, lost_turn_backoff=backoff)
    run = start(role, restarting=restarting, view=SUSPENDING)
    run.advance_to(0.0)

    run.resume_ready(run.suspend(turn(run)))
    asked = turn(run)
    assert spec_of(asked).charge_class == "resume"
    assert asked.template == EXCHANGES.resume

    run.lose(asked)
    if lost_turns > 0:
        assert min(run.sleeping_until) == backoff.delay(0)  # the others are the deadline
        run.advance_to(backoff.delay(0))
        again = turn(run)
        assert again.template == asked.template
        assert again.context == asked.context
        assert spec_of(again).charge_class == "correction"
        assert spec_of(again).predecessor == invocation_of(asked)
        run.lose(again)
    assert published(run) == Outcome(ended="Failed", reason="lost")
    assert min(run.sleeping_until, default=1000.0) >= 1000.0  # no backoff: only the deadline


@_RESTARTING
def test_a_wait_core_will_never_resume_ends_the_ask_with_a_typed_refusal(
    *, restarting: bool
) -> None:
    """Core refuses a resume its own dispatch check would refuse, instead of halting the run.

    The search hears a typed refusal and decides what follows; no resume turn is asked.
    """
    run = start(role_of(may_wait_for_evaluations=True), restarting=restarting, view=SUSPENDING)
    run.advance_to(0.0)

    run.resume_refused(run.suspend(turn(run)), core.ResumeRefusal.HISTORY_UNAVAILABLE)

    assert published(run) == Outcome(
        ended="Failed",
        reason=core.FailureReason.WAIT_REFUSED.value,
        refusal=core.WaitRefusal.NOT_RESUMABLE.value,
    )
    assert not [asked for asked in run.open if isinstance(asked.request, core.RequestTurn)]


# -- the checkpoint --------------------------------------------------------------------------------------


def _write_role(reply: type[BaseModel]) -> core.Role:
    return role_of(
        reply=reply,
        workspace=core.WorkspaceMode.ISOLATED_CHILD,
        access=core.Access.WRITE_CANDIDATE,
        charge=core.Charge.ATTEMPT,
    )


async def _in_workspace(ctx: core.Flow, role: core.Role) -> None:
    async with open_workspace(ctx, "w") as workspace:
        if isinstance(workspace, core.NotOpened):
            return
        session = ctx.agents.session(role, workspace=workspace, revision=ctx.facts.baseline)
        ctx.publish(outcome_of(await session.turn(Prompt(text="go"))))


def _write_run(reply: type[BaseModel], *, restarting: bool) -> FlowRun[Config]:
    role = _write_role(reply)

    async def root(ctx: core.Flow, config: Config) -> None:
        del config
        await _in_workspace(ctx, role)

    run = start(role, restarting=restarting, definition=root)
    run.open_workspace(run.expect(core.StartAttempt))
    return run


@_RESTARTING
def test_a_reply_that_claims_work_is_held_until_core_knows_the_checkpoint(
    *, restarting: bool
) -> None:
    run = _write_run(Change, restarting=restarting)
    asked = turn(run)
    assert spec_of(asked).charge_class == "paid"

    run.reply(asked, Change(n=1))
    assert published(run) is None

    kept = core.Retained(revision=core.RevisionRef.of_git_commit("abc"))
    run.send(core.CheckpointKnown(invocation=invocation_of(asked), checkpoint=kept))
    assert published(run) == Outcome(ended="TurnFinished", checkpoint=True)


@_RESTARTING
def test_a_reply_that_claims_no_work_is_delivered_at_once(*, restarting: bool) -> None:
    run = _write_run(Idle, restarting=restarting)

    run.reply(turn(run), Idle(n=1))

    assert published(run) == Outcome(ended="TurnFinished", checkpoint=False)


class Held(core.Value):
    """The checkpoint a write turn finished with."""

    checkpoint: core.Retained | core.Declined | None


@_SETTINGS
@given(known=st.sampled_from(["retained", "declined"]))
@_RESTARTING
def test_a_write_turn_that_waited_is_held_until_core_knows_the_checkpoint_of_its_resume(
    *, restarting: bool, known: str
) -> None:
    """The turn that waited is suspended with its work; the resumed turn ends with the checkpoint.

    Core records the checkpoint of the suspended turn (kept or declined). The resumed turn only
    reads the verdict, but its reply claims the work again, so it is held like any write turn's
    until core knows the checkpoint, and then ends with exactly that checkpoint.
    """
    role = _write_role(Change).model_copy(update={"may_wait_for_evaluations": True})
    checkpoint: core.Retained | core.Declined = (
        core.Retained(revision=core.RevisionRef.of_git_commit("abc"))
        if known == "retained"
        else core.Declined(reason=next(iter(core.CheckpointDecline)))
    )

    async def root(ctx: core.Flow, config: Config) -> None:
        del config
        async with open_workspace(ctx, "w") as workspace:
            if isinstance(workspace, core.NotOpened):
                return
            session = ctx.agents.session(role, workspace=workspace, revision=ctx.facts.baseline)
            result = await session.turn(Prompt(text="go"))
            assert isinstance(result, core.TurnFinished)
            ctx.publish(Held(checkpoint=result.checkpoint))

    run = start(role, restarting=restarting, view=SUSPENDING, definition=root)
    run.open_workspace(run.expect(core.StartAttempt))
    asked = turn(run)
    waiting = run.suspend(asked)
    run.send(core.CheckpointKnown(invocation=invocation_of(asked), checkpoint=checkpoint))
    run.resume_ready(waiting)
    resumed = turn(run)
    assert spec_of(resumed).charge_class == "resume"
    run.reply(resumed, Change(n=1))
    assert run.driver.published(run.state, Held) is None
    run.send(core.CheckpointKnown(invocation=invocation_of(resumed), checkpoint=checkpoint))

    assert run.driver.published(run.state, Held) == Held(checkpoint=checkpoint)


# -- the roles a search may ask ----------------------------------------------------------------------------


def _asking(other: core.Role) -> Callable[[core.Flow, Config], Coroutine[Any, Any, None]]:
    async def root(ctx: core.Flow, config: Config) -> None:
        del config
        await ctx.agents.session(other).turn(Prompt(text="go"))

    return root


def test_a_role_the_search_does_not_declare_cannot_be_asked() -> None:
    with pytest.raises(core.ContractError, match="not declared"):
        start(role_of(), definition=_asking(role_of(name="stranger")))


def test_a_role_that_differs_from_the_declared_one_cannot_be_asked() -> None:
    with pytest.raises(core.ContractError, match="differs"):
        start(role_of(), definition=_asking(role_of(corrections=2)))


# -- the operator's model ---------------------------------------------------------------------------------


@given(model=st.none() | st.sampled_from(["m-1", "m-2"]))
def test_the_model_an_operator_chose_reaches_the_turn_and_only_that_role(
    model: str | None,
) -> None:
    role = role_of()

    async def root(ctx: core.Flow, config: Config) -> None:
        del config
        # The flow names its own role; the run's declaration carries the chosen model.
        await ctx.agents.session(role).turn(Prompt(text="go"))

    definition = fake_search("govern", Config, (role,))(root)
    chosen = definition if model is None else definition.with_models({"worker": model})
    run = FlowRun.start(chosen, Config())

    assert spec_of(turn(run)).session.model == model


# -- a role that puts another question to an agent a role declares -----------------------------------------


@given(model=st.text(alphabet="abcdefgh-", min_size=1, max_size=10))
@_RESTARTING
def test_a_role_asked_of_another_roles_agent_is_that_agents_id_and_model(
    *, restarting: bool, model: str
) -> None:
    designer = role_of(name="designer")
    router = role_of(name="router", agent="designer")

    async def root(ctx: core.Flow, config: Config) -> None:
        del config
        await ctx.agents.session(router).turn(Prompt(text="go"))

    definition = fake_search("govern", Config, (designer, router))(root)
    chosen = definition.with_models({"designer": model})
    run = FlowRun.start(chosen, Config(), restarting=restarting)

    asked = turn(run)
    assert spec_of(asked).session.role_id == chosen.ids.role_id("designer")
    assert spec_of(asked).session.model == model
    assert asked.decision.root.startswith("router")
    with pytest.raises(core.UnknownRoleError):
        definition.with_models({"router": model})


@pytest.mark.regression
@example(interruptions=3)
@given(interruptions=st.integers(1, 12))
@_RESTARTING
def test_epoch_interruption_never_spends_a_roles_correction_or_lost_turn(
    *, restarting: bool, interruptions: int
) -> None:
    run = start(role_of(lost_turns=0, corrections=0), restarting=restarting)
    asked = turn(run)
    for _ in range(interruptions):
        run.fail_turn(asked, core.TurnFailureKind.EPOCH_INTERRUPTED)
        again = turn(run)
        assert again.template == asked.template
        assert again.context == asked.context
        assert spec_of(again).charge_class == spec_of(asked).charge_class
        assert spec_of(again).predecessor == invocation_of(asked)
        assert run.sleeping_until == ()
        asked = again
    run.lose(asked)
    assert published(run) == Outcome(ended="Failed", reason="lost")
