"""The run's main revision: new work starts from it, and only a merge that merged moves it.

Main starts at the run's input. A merge workstream has a new merger session merge candidates on
a checkout of main; its commit is measured with the trusted evaluations, and main moves to it
when it passes accuracy. A refusal, a failed turn or a commit that fails accuracy leaves main
where it was. These tests play the search under a fake host, live and with restarts: generated
stories in which the planner merges whatever is mergeable, and named stories for each outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hypothesis import event, given, note, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._host import PLANNER_LABEL, Host, begin, config
from tests.vibesys.orchestration.dynamic._replies import implement, merge, merged, plan_reply
from tests.vibesys.orchestration.dynamic._slots import (
    implement_and_judge,
    open_next_slot,
    planner_context,
    turn_about,
    verify_parents,
)
from tests.vibesys.orchestration.dynamic.core_policy._contexts import render
from tests.vibesys.search_faults import Step, play, steps
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.orchestration.dynamic.models import MergeResult
from vibesys.orchestration.dynamic.search.api import (
    ImplementPrompt,
    MergeOutcome,
    MergePrompt,
    PlannerPrompt,
    PromptTemplate,
    RunSnapshot,
    WorkKind,
)
from vs_core.api import (
    Measure,
    Operation,
    RequestTurn,
    Retained,
    RevisionRef,
    StartAttempt,
    Withdraw,
)
from vs_sim.api.testing import examples

MODES = across_restarts


@dataclass
class MainWatch:
    """What the stories saw of main as the search published its record."""

    mains: list[RevisionRef] = field(default_factory=list)
    """Main at each publication, in order; the first is the run's input."""
    seen: set[int] = field(default_factory=set)
    """The sequences of the workstreams already checked."""

    def check(self, record: RunSnapshot | None) -> None:
        """Main moves only to the commit of a merge that merged; new work starts from main."""
        if record is None or record.main is None:
            return
        before = self.mains[-1] if self.mains else record.main
        self.mains.append(record.main)
        merged_ = [item.revision for item in record.merges if item.outcome is MergeOutcome.MERGED]
        # Main is the commit of the last merge that merged, else still the input.
        assert record.main == (merged_[-1] if merged_ else self.mains[0]), record.merges
        moves = sum(
            1 for prior, now in zip(self.mains, self.mains[1:], strict=False) if now != prior
        )
        assert moves <= len(merged_)
        others = [item for item in record.merges if item.outcome is not MergeOutcome.MERGED]
        assert all(item.revision not in self.mains for item in others if item.revision)
        for running in record.running:
            if running.sequence in self.seen:
                continue
            self.seen.add(running.sequence)
            plan = running.plan
            fresh = plan.kind is WorkKind.MERGE or (
                plan.kind is WorkKind.IMPLEMENT
                and not plan.continue_hypothesis
                and plan.parent_hypothesis_id is None
            )
            if fresh:
                # Scheduled in this turn of the root or the one before: main then or now.
                assert running.parent in {before, record.main}, (running, before, record.main)


@MODES
@settings(max_examples=examples(pr=15, nightly=100), deadline=None, derandomize=True, database=None)
@given(
    story=steps(),
    rounds=st.integers(2, 4),
    in_flight=st.integers(1, 2),
    throughputs=st.dictionaries(
        st.sampled_from([f"c{n}" for n in range(1, 12)]), st.floats(10, 200), max_size=11
    ),
    answers=st.lists(st.integers(0, 3), max_size=4),
)
def test_main_moves_only_through_a_merge_that_merged_and_new_work_starts_from_it(
    *,
    restarting: bool,
    story: list[Step],
    rounds: int,
    in_flight: int,
    throughputs: dict[str, float],
    answers: list[int],
) -> None:
    host = begin(
        config(max_rounds=rounds, max_in_flight=in_flight),
        restarting=restarting,
        merging=True,
        throughputs=throughputs,
        merger_answers=answers,
    )
    watch = MainWatch()
    try:
        play(host, story, check=lambda: watch.check(host.record))
    finally:
        note(host.run.timeline())
    watch.check(host.record)
    record = host.record
    outcomes = sorted({item.outcome.value for item in record.merges}) if record else []
    event(f"merge outcomes: {outcomes}")
    # Each merge ran in its own new session: no session served two merges.
    sessions = [session for found in host.merger_sessions.values() for session in found]
    assert len(sessions) == len(set(sessions)), host.merger_sessions
    assert all(len(found) == 1 for found in host.merger_sessions.values()), host.merger_sessions


# -- named stories -----------------------------------------------------------------------------


def _start(host: Host) -> None:
    """Verify the parent, when it is not the input, then admit the one workstream waiting."""
    for _ in range(3):
        if host.open(StartAttempt):
            break
        verify_parents(host)
    assert host.open(StartAttempt), (host.run.timeline(), [a.label for a in host.run.open])
    open_next_slot(host)


def _implemented(host: Host, identifier: str, candidate: str, outcome: str = "supported") -> None:
    """Play one implement workstream through its measurement and settlement."""
    _start(host)
    implement_and_judge(host, identifier, candidate, outcome=outcome)
    _settle(host)


def _settle(host: Host) -> None:
    """Answer the measurement, the reading of its evidence and the close, as the world would."""
    for _ in range(10):
        pending = [*host.open(Measure), *host.open(Operation), *host.open(Withdraw)]
        if not pending:
            return
        host.answer(pending[0])


def _merger(host: Host) -> RequestTurn:
    (asked,) = host.open(RequestTurn, label="merger")
    assert isinstance(asked.request, RequestTurn)
    return asked.request


def _merge(host: Host, reply: str, commit: str | None) -> MergePrompt:
    """Admit the merge workstream and have its merger answer ``reply``, committing ``commit``."""
    _start(host)
    (asked,) = host.open(RequestTurn, label="merger")
    context = asked.context
    assert isinstance(context, MergePrompt)
    checkpoint = None if commit is None else Retained(revision=RevisionRef.of_git_commit(commit))
    host.run.reply(asked, MergeResult.model_validate_json(reply), checkpoint=checkpoint)
    _settle(host)
    return context


def _plan(host: Host, *entries: dict[str, object]) -> PlannerPrompt:
    host.drain_until_turn(PLANNER_LABEL)
    context = planner_context(host)
    assert isinstance(context, PlannerPrompt)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(*entries))
    return context


def _first_candidate(*, restarting: bool, **world: object) -> Host:
    """A run whose first workstream made candidate ``c1`` of ``h1``, beating the input."""
    host = begin(config(max_rounds=5), restarting=restarting, **world)  # ty: ignore[invalid-argument-type]
    first = _plan(host, implement("h1"))
    assert first.main_revision == first.base_revision
    assert first.mergeable == ()
    _implemented(host, "h1", "c1")
    return host


@MODES
def test_a_merge_that_merged_moves_main_and_the_next_workstream_starts_from_it(
    *, restarting: bool
) -> None:
    host = _first_candidate(restarting=restarting)
    context = _plan(host, merge("m1", "h1"))
    assert [row.hypothesis_id for row in context.mergeable] == ["h1"]
    prompt = _merge(host, merged(), "c2")
    assert [source.hypothesis_id for source in prompt.candidates] == ["h1"]
    assert prompt.main_revision == context.main_revision  # merged on a checkout of main
    after = _plan(host, implement("h2"))
    main = RevisionRef.of_git_commit("c2")
    assert after.main_revision == main
    assert after.main_measurement is not None
    assert after.main_measurement.merge_id == "m1"
    assert not after.main_measurement.regressed
    assert after.mergeable == ()  # h1 is in main now, and nothing else beats it
    (record,) = after.merges
    assert record.outcome is MergeOutcome.MERGED
    _start(host)
    started = turn_about(host, "implementer", "h2").context
    assert isinstance(started, ImplementPrompt)
    assert started.parent_revision == main


@MODES
def test_a_refused_merge_leaves_main_unchanged_and_the_planner_hears_why(
    *, restarting: bool
) -> None:
    host = _first_candidate(restarting=restarting)
    before = _plan(host, merge("m1", "h1"))
    _merge(host, merged("refused", "h1 rewrites the kernel the instructions keep"), None)
    after = _plan(host, implement("h2"))
    assert after.main_revision == before.main_revision == before.base_revision
    (record,) = after.merges
    assert record.outcome is MergeOutcome.REFUSED
    assert record.reason == "h1 rewrites the kernel the instructions keep"
    text = render(PromptTemplate.PORTFOLIO, after)
    assert "h1 rewrites the kernel the instructions keep" in text
    assert [row.hypothesis_id for row in after.mergeable] == ["h1"]  # still mergeable


@MODES
def test_a_merge_whose_turn_failed_leaves_main_unchanged(*, restarting: bool) -> None:
    host = _first_candidate(restarting=restarting)
    before = _plan(host, merge("m1", "h1"))
    _start(host)
    for _ in range(3):  # the turn, then core's asks again after a lost turn
        merger = host.open(RequestTurn, label="merger")
        if not merger:
            break
        host.run.lose(merger[0])
        if host.run.sleeping_until:
            host.run.advance_to(min(host.run.sleeping_until))
    _settle(host)
    after = _plan(host, implement("h2"))
    assert after.main_revision == before.main_revision
    (record,) = after.merges
    assert record.outcome is MergeOutcome.FAILED
    assert record.failure is not None


@MODES
def test_a_merged_commit_that_measures_worse_moves_main_and_is_reported_as_a_regression(
    *, restarting: bool
) -> None:
    host = _first_candidate(restarting=restarting, throughputs={"c1": 300.0, "c2": 20.0})
    before = _plan(host, merge("m1", "h1"))
    _merge(host, merged(), "c2")
    after = _plan(host, implement("h2"))
    assert after.main_revision == RevisionRef.of_git_commit("c2") != before.main_revision
    measured = after.main_measurement
    assert measured is not None
    assert measured.accuracy_passed
    assert measured.regressed  # 20 is worse than main before the merge (the input's 50)
    assert [row.value for row in measured.previous] == [50.0]
    text = " ".join(render(PromptTemplate.PORTFOLIO, after).split())
    assert "Main regressed with that merge" in text


@MODES
def test_a_hypothesis_that_goes_on_after_main_moved_is_asked_to_rebase_onto_main(
    *, restarting: bool
) -> None:
    host = _first_candidate(restarting=restarting)
    _plan(host, implement("h2"))
    _implemented(host, "h2", "c2", "inconclusive")  # it may go on
    _plan(host, merge("m1", "h1"))
    _merge(host, merged(), "c3")
    context = _plan(host, implement("h2", continue_hypothesis=True))
    assert context.main_revision == RevisionRef.of_git_commit("c3")
    _start(host)
    resumed = turn_about(host, "implementer", "h2").context
    assert isinstance(resumed, ImplementPrompt)
    assert resumed.parent_revision == RevisionRef.of_git_commit("c2")  # its own work
    assert resumed.rebase_onto == context.main_revision
    text = " ".join(render(PromptTemplate.IMPLEMENT, resumed).split())
    assert "rebase your work onto it" in text


@MODES
def test_a_hypothesis_started_on_main_is_not_asked_to_rebase(*, restarting: bool) -> None:
    host = _first_candidate(restarting=restarting)
    _plan(host, merge("m1", "h1"))
    _merge(host, merged(), "c2")
    _plan(host, implement("h2"))
    _implemented(host, "h2", "c3", "inconclusive")  # it may go on
    _plan(host, implement("h2", continue_hypothesis=True))
    _start(host)
    resumed = turn_about(host, "implementer", "h2").context
    assert isinstance(resumed, ImplementPrompt)
    assert resumed.rebase_onto is None
