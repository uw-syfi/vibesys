"""The evolve flow told as a story, by hand: no core, no clock, no event loop.

Each story plays the host for `evolve`: it answers the waits the search opens, in an order the
story chooses, and reads what the search decided. Every story runs twice, the second time with a
restart after every step: the search's state is round-tripped through JSON and the search rebuilt
by replay, and the decisions must not change. What core itself guarantees about these waits
(retry, recovery, release) is core's own tests; what is asserted here is only what the search
decided.
"""

from __future__ import annotations

from tests.vibesys.orchestration.evolve._host import Host, begin, config
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.orchestration.evolve.search.api import (
    ExportPatch,
    JudgePrompt,
    MutatePrompt,
    ProfilerPrompt,
    Stage,
)
from vs_core.api import (
    Finish,
    Measure,
    Operation,
    RequestTurn,
    StartAttempt,
    Wait,
    Withdraw,
    WorkspaceMode,
    WorkspaceRef,
)

MODES = across_restarts


def adopted(host: Host) -> str | None:
    """The commit the run adopted; None when it ended with no winner."""
    finish = host.run.finished
    assert isinstance(finish, Finish)
    chosen = finish.result.selection
    return None if chosen is None else chosen.revision.git_commit


def individuals(host: Host) -> tuple:
    record = host.record
    assert record is not None
    return record.population.individuals


def mutate_context(host: Host) -> MutatePrompt:
    """The brief of the mutator turn that is open now."""
    context = host.run.expect_turn(role="mutator").context
    assert isinstance(context, MutatePrompt)
    return context


@MODES
def test_a_cold_start_then_a_better_child_is_adopted(*, restarting: bool) -> None:
    host = begin(config(), restarting=restarting, throughputs={"c1": 100.0, "c2": 150.0})
    host.drain()

    assert adopted(host) == "c2"
    seeded, child = individuals(host)
    assert (seeded.parent_id, seeded.passed) == (None, True)
    assert (child.parent_id, child.passed, child.perf_metric) == (seeded.id, True, 150.0)
    assert not host.unsettled


@MODES
def test_a_run_in_the_hosts_root_asks_for_the_root_and_the_mutator_works_there(
    *, restarting: bool
) -> None:
    host = begin(config(exclusive_root=True), restarting=restarting)
    host.drain()

    modes = {
        item.request.workspace.mode
        for item in host.run.decisions
        if isinstance(item, Wait) and isinstance(item.request, StartAttempt)
    }
    assert modes == {WorkspaceMode.EXCLUSIVE_ROOT}
    mutator_modes = {
        item.request.turn.workspace.mode
        for item in host.run.decisions
        if isinstance(item, Wait)
        and isinstance(item.request, RequestTurn)
        and item.request.turn.session.role_id.root.endswith("mutator")
        and isinstance(item.request.turn.workspace, WorkspaceRef)
    }
    assert mutator_modes == {WorkspaceMode.EXCLUSIVE_ROOT}
    assert adopted(host) is not None
    assert not host.unsettled


@MODES
def test_a_child_that_does_not_beat_its_parent_leaves_the_parent_the_winner(
    *, restarting: bool
) -> None:
    host = begin(config(), restarting=restarting, throughputs={"c1": 100.0, "c2": 60.0})
    host.drain()

    assert adopted(host) == "c1"
    assert [item.passed for item in individuals(host)] == [True, True]


@MODES
def test_a_failed_cold_start_is_repaired_in_place_by_the_next_one(*, restarting: bool) -> None:
    host = begin(config(), restarting=restarting)
    host.drain_until_turn("mutator")
    first = mutate_context(host)
    assert (first.is_cold_start, first.repair_seed, first.failed_lessons) == (True, False, ())
    host.answer(host.run.expect_turn(role="mutator"))  # leaves c1
    host.answer(host.run.expect_turn(role="judge"), variant=1)  # rejects it
    settle = host.run.expect(Withdraw, label="settle")
    assert settle.request.disposition.retention == "wip"  # ty: ignore[unresolved-attribute]
    assert settle.request.disposition.candidate.git_commit == "c1"  # ty: ignore[unresolved-attribute]
    host.drain_until_turn("mutator")
    (reopened,) = [a for a in host.run.open if isinstance(a.request, StartAttempt)] or [None]
    assert reopened is None or reopened.request.workspace.base.git_commit == "c1"  # ty: ignore[unresolved-attribute]

    second = mutate_context(host)
    assert second.repair_seed
    assert second.base_revision.git_commit == "c1"
    assert second.failed_lessons == ("needs work",)
    assert second.num_failed_attempts == 1
    (failed,) = individuals(host)
    assert (failed.passed, failed.parent_id, failed.commit) == (False, None, "c1")
    assert host.record is not None
    assert host.record.stage is Stage.BOOTSTRAP


@MODES
def test_cold_starts_that_never_pass_end_the_run_with_no_winner(*, restarting: bool) -> None:
    host = begin(config(), restarting=restarting)
    for _ in range(2):
        host.drain_until_turn("mutator")
        host.answer(host.run.expect_turn(role="mutator"))
        host.answer(host.run.expect_turn(role="judge"), variant=1)
    host.drain()

    assert adopted(host) is None
    assert len(individuals(host)) == 2
    assert len(host.opened) == 2  # the bootstrap budget, and no generation after it
    assert not host.unsettled


@MODES
def test_a_failed_gate_makes_a_failed_individual_carrying_the_gates_feedback(
    *, restarting: bool
) -> None:
    host = begin(config(bootstrap_max_attempts=1), restarting=restarting, failing={"c1"})
    host.drain()

    (member,) = individuals(host)
    assert (member.passed, member.feedback) == (False, "accuracy gate failed")
    assert adopted(host) is None


@MODES
def test_a_mutation_is_never_asked_again_and_a_lost_one_is_a_failed_individual(
    *, restarting: bool
) -> None:
    host = begin(config(bootstrap_max_attempts=1), restarting=restarting)
    host.drain_until_turn("mutator")
    host.answer(host.run.expect_turn(role="mutator"), variant=2)  # the provider drops the turn
    host.drain()

    (member,) = individuals(host)
    assert not member.passed
    assert "the mutator returned no valid response" in member.feedback
    assert not host.open(object, label="mutator")


@MODES
def test_a_declined_checkpoint_fails_the_individual_without_a_candidate(
    *, restarting: bool
) -> None:
    host = begin(config(bootstrap_max_attempts=1), restarting=restarting)
    host.drain_until_turn("mutator")
    host.answer(host.run.expect_turn(role="mutator"), variant=1)
    host.drain()

    (member,) = individuals(host)
    assert (member.passed, member.commit) == (False, None)
    assert "checkpoint declined" in member.feedback


@MODES
def test_the_judge_reviews_the_revision_the_mutator_left(*, restarting: bool) -> None:
    host = begin(config(bootstrap_max_attempts=1), restarting=restarting)
    host.drain_until_turn("mutator")
    host.answer(host.run.expect_turn(role="mutator"))
    review = host.run.expect_turn(role="judge").context
    assert isinstance(review, JudgePrompt)
    assert review.candidate.git_commit == "c1"


@MODES
def test_the_child_of_a_generation_builds_on_a_verified_parent_and_sees_it(
    *, restarting: bool
) -> None:
    host = begin(config(), restarting=restarting)
    host.drain_until_turn("mutator")
    host.answer(host.run.expect_turn(role="mutator"))
    while not [a for a in host.run.open if a.label == "mutator" and "g1" in a.path]:
        assert host.step(0), host.run.timeline()

    prompt = mutate_context(host)
    assert not prompt.is_cold_start
    assert prompt.base_revision.git_commit == "c1"
    assert prompt.parent is not None
    assert prompt.parent.commit == "c1"


@MODES
def test_a_parent_that_no_longer_reproduces_drops_the_slot_without_an_individual(
    *, restarting: bool
) -> None:
    host = begin(config(), restarting=restarting, verified=False)
    host.drain()

    assert adopted(host) == "c1"
    assert len(individuals(host)) == 1  # the child never got a workspace
    assert len(host.opened) == 1


@MODES
def test_a_profiler_that_fails_is_a_note_and_the_child_still_passes(*, restarting: bool) -> None:
    host = begin(config(bootstrap_max_attempts=1, profiling=True), restarting=restarting)
    host.drain_until_turn("profiler")
    context = host.run.expect_turn(role="profiler").context
    assert isinstance(context, ProfilerPrompt)
    host.answer(host.run.expect_turn(role="profiler"), variant=1)
    host.drain()

    member = individuals(host)[0]
    assert member.passed
    assert host.record is not None
    assert host.record.notes
    assert host.record.notes[0].startswith("profiler: ")


@MODES
def test_the_profilers_extra_metrics_join_the_trusted_headline(*, restarting: bool) -> None:
    host = begin(config(bootstrap_max_attempts=1, profiling=True), restarting=restarting)
    host.drain()

    member = individuals(host)[0]
    assert member.perf_metric == 100.0
    assert member.metric_map() == {"cache_hit": 0.5, "throughput": 100.0}


@MODES
def test_the_openevolve_selector_exports_the_patch_of_a_passing_child(*, restarting: bool) -> None:
    host = begin(config(search_policy="openevolve"), restarting=restarting)
    host.drain()

    exports = [item for item in _operations(host) if isinstance(item.request, ExportPatch)]
    assert len(exports) == 2  # each passing child, the cold start and the generation's
    assert adopted(host) is not None


@MODES
def test_a_patch_export_that_fails_fails_the_child(*, restarting: bool) -> None:
    host = begin(
        config(search_policy="openevolve", bootstrap_max_attempts=1),
        restarting=restarting,
        export_status="failed",
    )
    host.drain()

    (member,) = individuals(host)
    assert not member.passed
    assert "patch export failed" in member.feedback


@MODES
def test_a_stop_while_the_mutator_works_settles_the_attempt_cancelled(*, restarting: bool) -> None:
    host = begin(config(), restarting=restarting)
    host.drain_until_turn("mutator")
    host.run.stop()
    settle = host.run.expect(Withdraw, label="settle")
    host.drain()

    assert isinstance(settle.request, Withdraw)
    assert settle.request.disposition.outcome == "cancelled"  # ty: ignore[unresolved-attribute]
    assert isinstance(host.run.finished, Finish)
    assert not host.unsettled
    assert individuals(host) == ()
    assert adopted(host) is None


@MODES
def test_a_generation_runs_its_children_together_and_admits_them_in_slot_order(
    *, restarting: bool
) -> None:
    host = begin(
        config(children_per_generation=2, max_parallelism=2),
        restarting=restarting,
        throughputs={"c2": 110.0, "c3": 120.0},
    )
    while len(host.open(StartAttempt)) < 2:
        assert host.step(0), host.run.timeline()
    for asked in host.open(StartAttempt):
        host.answer(asked)
    assert len(host.unsettled) == 2
    second = [a for a in host.run.open if "c2" in a.path and a.label == "mutator"]
    assert second
    # The second slot finishes first: its outcome waits for the first to be admitted.
    while any("c2" in a.path for a in host.run.open if a.until is not None):
        pending = [a for a in host.run.open if "c2" in a.path and a.until is not None]
        host.answer(pending[0])
    assert len(individuals(host)) == 1
    host.drain()

    seeded, first, second_child = individuals(host)
    assert [seeded.parent_id, first.parent_id, second_child.parent_id] == [
        None,
        seeded.id,
        seeded.id,
    ]
    assert [item.id for item in individuals(host)] == [1, 2, 3]
    assert not host.unsettled


def _operations(host: Host) -> list[Operation]:
    """Every operation the flow asked for, in order."""
    waits = (item for item in host.run.decisions if isinstance(item, Wait))
    return [item.request for item in waits if isinstance(item.request, Operation)]


@MODES
def test_a_start_core_refuses_leaves_the_child_without_an_individual_and_the_run_ends(
    *, restarting: bool
) -> None:
    host = begin(config(), restarting=restarting, refusals={"start": 0})
    host.drain_until_turn("mutator")
    host.answer(host.run.expect_turn(role="mutator"))
    host.refusals["start"] = 1  # the generation's child is refused its workspace
    host.drain()

    assert adopted(host) == "c1"
    assert len(individuals(host)) == 1
    assert not host.unsettled


@MODES
def test_a_failed_review_skips_the_trusted_gates_and_keeps_the_feedback(
    *, restarting: bool
) -> None:
    host = begin(config(bootstrap_max_attempts=1), restarting=restarting)
    host.drain_until_turn("mutator")
    host.answer(host.run.expect_turn(role="mutator"))
    host.answer(host.run.expect_turn(role="judge"), variant=1)
    host.drain()

    (member,) = individuals(host)
    assert member.feedback == "needs work"
    assert not [
        d for d in host.run.decisions if isinstance(d, Wait) and isinstance(d.request, Measure)
    ]


@MODES
def test_a_candidate_the_machinery_could_not_measure_earns_no_individual(
    *, restarting: bool
) -> None:
    host = begin(config(), restarting=restarting, unmeasurable=True)
    host.drain()

    assert individuals(host) == ()
    assert len(host.opened) == 2  # both cold starts were spent, and no generation followed
    assert adopted(host) is None
    assert not host.unsettled


@MODES
def test_a_run_without_a_profiler_asks_no_profiler_turn_and_one_with_asks_one_per_passing_child(
    *, restarting: bool
) -> None:
    quiet = begin(config(), restarting=restarting)
    quiet.drain()
    profiled = begin(config(children_per_generation=2, profiling=True), restarting=restarting)
    profiled.drain()

    def asked(host: Host) -> list[str]:
        waits = (d for d in host.run.decisions if isinstance(d, Wait))
        roots = (w.request.decision_id.root for w in waits)
        return [root for root in roots if root.rpartition("/")[2].startswith("profiler.1")]

    assert asked(quiet) == []
    assert len(asked(profiled)) == 3  # the cold start and two children
