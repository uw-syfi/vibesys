"""Public-API histories exercise one scheduler in live and reconstruction modes."""

from collections.abc import Callable, Coroutine

import pytest
from hypothesis import given
from hypothesis import strategies as st
from protobuf.wkt.google.protobuf.any_pb import Any

from vs_core.api.replay import (
    AcceptedClock,
    AcceptedReplay,
    ReplayAcknowledgements,
    ReplayCall,
    ReplayConsumerError,
    ReplayDeadlockError,
    ReplayDivergenceError,
    ReplayError,
    ReplayFlow,
    ReplayScheduler,
    ReplaySettings,
    SemanticKey,
    classify_context,
)
from vs_proto.api import core_common, replay

from .generated.vibesys.replay.testing.v1.context_pb import Context
from .replay_model import LinearCausality

Reply = core_common.Identity
Root = Callable[[ReplayFlow], Coroutine[object, object, object]]


def settings(fresh: int = 10) -> ReplaySettings:
    initial = replay.ReplayAllowance(iterations=10, capacity=2, remaining=10).to_binary()
    return ReplaySettings(
        0,
        initial,
        replay.ReplayAllowance(iterations=fresh, capacity=1, remaining=fresh).to_binary(),
    )


def ask(key: str, candidate: str = "candidate-a", policy: bytes = b"old") -> ReplayCall[Reply]:
    return ReplayCall(
        SemanticKey(key),
        Reply(root=candidate),
        "plan.v1",
        replay.ReplayPolicy(
            role="planner", template="plan.j2", content=policy, reply_type=Reply.desc().type_name
        ),
        Reply,
    )


def accepted(
    start: replay.OperationReplay, seq: int, value: str, *, clock: float | None = None
) -> AcceptedReplay:
    start.outcome = Any(
        type_url="type.googleapis.com/" + Reply.desc().type_name,
        value=Reply(root=value).to_binary(),
    )
    start.accepted_sequence = seq
    start.paid = True
    start.cost_usd = 0.5
    return AcceptedReplay(seq, float(seq) if clock is None else clock, start.to_binary())


def finish(run: ReplayScheduler, values: list[str]) -> list[AcceptedReplay]:
    history = []
    for sequence, value in enumerate(values, 1):
        (start,) = run.dispatches()
        observation = accepted(start, sequence, value)
        history.append(observation)
        run.accept(observation)
    return history


@given(
    values=st.lists(st.sampled_from(["a", "b", "c"]), min_size=1, max_size=25),
    fresh=st.integers(min_value=0, max_value=100),
)
def test_fold_history_reuses_after_prompt_allowance_and_helper_edits(
    values: list[str], fresh: int
) -> None:
    def root(policy: bytes) -> Root:
        async def helper(ctx: ReplayFlow, i: int) -> Reply:
            return await ctx.call(ask(str(i), policy=policy))

        async def flow(ctx: ReplayFlow) -> object:
            observed = []
            for index in range(len(values)):
                reply = await helper(ctx, index)
                observed.append((reply.root, ctx.clock(), ctx.allowance().remaining))
            return tuple(observed)

        return flow

    live = ReplayScheduler(root(b"old"), (), settings())
    history = finish(live, values)
    replayed = ReplayScheduler(root(b"new prompt"), history, settings(fresh))
    assert replayed.result == live.result
    assert replayed.dispatches() == ()
    assert replayed.frontier().unused == ()
    assert replayed.frontier().drift.count == len(values)
    assert replayed.frontier().drift.roles == ("planner",)
    assert replayed.frontier().drift.templates == ("plan.j2",)
    assert replayed.allowance_bytes == settings(fresh).fresh_allowance


def fanout(ctx_observations: list[tuple[str, float, int, int]]) -> Root:
    async def root(ctx: ReplayFlow) -> object:
        channel = ctx.channel()
        active = 2
        done = 0
        group = ctx.group()

        async def worker(child: ReplayFlow, name: str) -> None:
            result = await child.call(ask(name))
            channel.send(result.root)

        async def a(child: ReplayFlow) -> None:
            await worker(child, "A")

        async def b(child: ReplayFlow) -> None:
            await worker(child, "B")

        group.start(a)
        group.start(b)
        first = await channel
        active -= 1
        done += 1
        ctx_observations.append((first, ctx.clock(), active, done))
        await ctx.call(ask("P"))
        second = await channel
        active -= 1
        done += 1
        ctx_observations.append((second, ctx.clock(), active, done))
        await group.all()
        return first, second

    return root


def test_fanout_releases_b_then_planning_then_a_with_quiescence() -> None:
    observations = []
    live = ReplayScheduler(fanout(observations), (), settings())
    a_start, b_start = live.dispatches()
    b = accepted(b_start, 40, "B", clock=8)
    live.accept(b)
    (p_start,) = [
        start for start in live.dispatches() if start.key is not None and start.key.operation == "P"
    ]
    p = accepted(p_start, 43, "plan", clock=9)
    live.accept(p)
    a = accepted(a_start, 47, "A", clock=11)
    live.accept(a)
    assert observations == [("B", 8, 1, 1), ("A", 11, 0, 2)]
    replay_observations = []
    replayed = ReplayScheduler(fanout(replay_observations), [a, p, b], settings())
    assert replay_observations == observations
    assert replayed.result == ("B", "A")
    assert replayed.dispatches() == ()


@pytest.mark.parametrize("edit", ["rename", "candidate"])
def test_unused_paid_frontier_has_one_notice_and_second_resume_proceeds(edit: str) -> None:
    async def old(ctx: ReplayFlow) -> object:
        await ctx.call(ask("first"))
        return await ctx.call(ask("second"))

    async def changed(ctx: ReplayFlow) -> object:
        await ctx.call(
            ask(
                "renamed" if edit == "rename" else "first",
                "candidate-b" if edit == "candidate" else "candidate-a",
            )
        )
        return await ctx.call(ask("second"))

    live = ReplayScheduler(old, (), settings())
    history = finish(live, ["a", "b"])
    paused = ReplayScheduler(changed, history, settings())
    frontier = paused.frontier()
    assert len(frontier.unused) == 2
    assert frontier.known_cost_usd == 1
    assert frontier.unknown_cost_count == 0
    assert paused.dispatches() == ()
    notice = frontier.notice()
    assert notice is not None
    acks = ReplayAcknowledgements().record_notice(notice)
    acknowledged = acks.resume("launch-2")
    assert acknowledged.resume("launch-2") == acknowledged
    resumed = ReplayScheduler(changed, history, settings(), acknowledged)
    assert resumed.frontier().unused == ()
    assert len(resumed.dispatches()) == 1
    resumed.close()
    paused.close()


def test_channels_latches_child_creation_and_group_joins_propagate_causality() -> None:
    async def root(ctx: ReplayFlow) -> object:
        gate = ctx.latch()
        group = ctx.group()

        async def producer(child: ReplayFlow) -> None:
            await child.call(ask("producer"))
            gate.set("ready")

        async def consumer(child: ReplayFlow) -> object:
            await gate
            return await child.call(ask("consumer"))

        group.start(producer)
        group.start(consumer)
        await group.all()
        return await ctx.call(ask("after-join"))

    live = ReplayScheduler(root, (), settings())
    history = finish(live, ["a", "b", "c"])
    replayed = ReplayScheduler(root, history, settings())
    assert replayed.result == live.result
    assert replayed.frontier().unused == ()
    assert len({item.identity.dependencies for item in history}) == 3


def test_equal_concurrent_requests_join_but_divergent_requests_fail() -> None:
    def root(*, divergent: bool) -> Root:
        async def flow(ctx: ReplayFlow) -> object:
            group = ctx.group()

            async def child_a(child: ReplayFlow) -> object:
                return await child.call(ask("same"))

            async def child_b(child: ReplayFlow) -> object:
                return await child.call(ask("same", "different" if divergent else "candidate-a"))

            group.start(child_a)
            group.start(child_b)
            return await group.all()

        return flow

    live = ReplayScheduler(root(divergent=False), (), settings())
    history = finish(live, ["a"])
    assert isinstance(live.result, tuple)
    assert len(live.result) == 2
    assert ReplayScheduler(root(divergent=False), history, settings()).result == live.result
    with pytest.raises(ReplayDivergenceError, match="concurrent divergent"):
        ReplayScheduler(root(divergent=True), (), settings())


def test_deadlocked_channels_are_diagnostics() -> None:
    async def root(ctx: ReplayFlow) -> object:
        return await ctx.channel()

    with pytest.raises(ReplayDeadlockError, match="no producer"):
        ReplayScheduler(root, (), settings())


def test_incompatible_historical_consumer_requires_contract_bump() -> None:
    async def root(ctx: ReplayFlow) -> object:
        return await ctx.call(ask("one"))

    live = ReplayScheduler(root, (), settings())
    (start,) = live.dispatches()
    observation = accepted(start, 1, "a")
    operation = observation.operation()
    assert operation.outcome is not None
    operation.outcome.type_url = "type.googleapis.com/other.Reply"
    incompatible = AcceptedReplay(1, 1, operation.to_binary())
    with pytest.raises(ReplayConsumerError, match="semantic data contract"):
        ReplayScheduler(root, [incompatible], settings())
    live.close()


def test_live_acceptance_rejects_reordering_and_duplicate_acceptance() -> None:
    async def root(ctx: ReplayFlow) -> object:
        await ctx.call(ask("one"))
        return await ctx.call(ask("two"))

    live = ReplayScheduler(root, (), settings())
    (one,) = live.dispatches()
    first = accepted(one, 4, "a")
    live.accept(first)
    with pytest.raises(ReplayError, match="no pending"):
        live.accept(first)
    (two,) = live.dispatches()
    with pytest.raises(ReplayError, match="advance log sequence"):
        live.accept(accepted(two, 3, "b"))
    live.close()


@given(order=st.permutations(["A", "B", "C", "D"]), fresh=st.integers(min_value=0, max_value=100))
def test_generated_fanout_prefixes_rebuild_every_channel_and_counter(
    order: list[str], fresh: int
) -> None:
    def root(observed: list[tuple[str, int, float, float]]) -> Root:
        async def flow(ctx: ReplayFlow) -> object:
            channel = ctx.channel()
            group = ctx.group()

            def worker(name: str) -> Root:
                async def child(child_ctx: ReplayFlow) -> object:
                    reply = await child_ctx.call(ask(name))
                    channel.send(reply.root)
                    return reply

                return child

            for name in ("A", "B", "C", "D"):
                group.start(worker(name))
            reports = []
            for count in range(len(order)):
                report = await channel
                observed.append((report, count, ctx.clock(), ctx.allowance().remaining))
                reports.append(report)
                await ctx.call(ask("planning." + report))
            await group.all()
            return tuple(reports)

        return flow

    observed = []
    live = ReplayScheduler(root(observed), (), settings())
    history = []
    for name in order:
        (start,) = [
            item
            for item in live.dispatches()
            if item.key is not None and item.key.operation == name
        ]
        item = accepted(start, len(history) + 1, name)
        live.accept(item)
        history.append(item)
        (start,) = [
            item
            for item in live.dispatches()
            if item.key is not None and item.key.operation == "planning." + name
        ]
        item = accepted(start, len(history) + 1, "plan")
        live.accept(item)
        history.append(item)
    assert live.result == tuple(order)
    for prefix in range(len(history) + 1):
        replay_observed = []
        reconstructed = ReplayScheduler(root(replay_observed), history[:prefix], settings(fresh))
        for item in history[prefix:]:
            starts = reconstructed.dispatches()
            assert any(
                start.identity == item.identity.canonical
                and SemanticKey.from_proto(start.key) == item.key
                for start in starts
                if start.key is not None
            )
            reconstructed.accept(item)
        assert reconstructed.result == live.result
        assert replay_observed == observed
        assert not reconstructed.frontier().unused


def test_reversing_causal_dependencies_is_a_miss_never_time_travel() -> None:
    def root(keys: tuple[str, ...]) -> Root:
        async def flow(ctx: ReplayFlow) -> object:
            result = None
            for key in keys:
                result = await ctx.call(ask(key))
            return result

        return flow

    live = ReplayScheduler(root(("A", "B")), (), settings())
    history = finish(live, ["a", "b"])
    changed = ReplayScheduler(root(("B", "A")), history, settings())
    notice = changed.frontier().notice()
    assert notice is not None
    assert len(changed.frontier().unused) == 2
    acks = ReplayAcknowledgements().record_notice(notice).resume("resume-2")
    resumed = ReplayScheduler(root(("B", "A")), history, settings(), acks)
    (start,) = resumed.dispatches()
    assert start.identity != history[1].identity.canonical
    resumed.accept(accepted(start, 3, "new-b"))
    (start,) = resumed.dispatches()
    assert start.identity != history[0].identity.canonical
    resumed.accept(accepted(start, 4, "new-a"))
    assert resumed.completed
    changed.close()


def test_timer_only_progress_is_recorded_and_first_uses_argument_order() -> None:

    async def root(ctx: ReplayFlow) -> object:
        later = ctx.timer(5)
        earlier = ctx.timer(3)
        index, value = await ctx.first(later, earlier)
        result = await ctx.call(ask("after-timer"))
        return index, value, ctx.clock(), result.root

    live = ReplayScheduler(root, (), settings())
    clock = AcceptedClock(1, 8, replay.ReplayAllowance(remaining=7).to_binary())
    assert live.clock_requests() == (3, 5)
    live.accept(clock)
    (start,) = live.dispatches()
    result = accepted(start, 2, "a", clock=9)
    live.accept(result)
    reconstructed = ReplayScheduler(root, [clock, result], settings())
    assert reconstructed.result == live.result == (0, 8, 9, "a")
    assert reconstructed.clock_requests() == ()


def test_skipped_observations_do_not_advance_visible_clock_or_allowance() -> None:
    async def old(ctx: ReplayFlow) -> object:
        return await ctx.call(ask("removed"))

    live = ReplayScheduler(old, (), settings())
    history = finish(live, ["a"])
    observed = []

    async def changed(ctx: ReplayFlow) -> object:
        observed.append((ctx.clock(), ctx.allowance().remaining))
        return await ctx.call(ask("new"))

    run = ReplayScheduler(changed, history, settings(2))
    assert observed == [(0, 10)]
    assert run.clock == 0
    assert run.allowance_bytes == settings(2).fresh_allowance
    run.close()


def test_joined_consumers_receive_independent_historical_messages() -> None:
    async def root(ctx: ReplayFlow) -> object:
        group = ctx.group()

        async def mutate(child: ReplayFlow) -> object:
            reply = await child.call(ask("shared"))
            reply.root = "changed"
            return reply.root

        async def read(child: ReplayFlow) -> object:
            return (await child.call(ask("shared"))).root

        group.start(mutate)
        group.start(read)
        return await group.all()

    live = ReplayScheduler(root, (), settings())
    history = finish(live, ["original"])
    assert live.result == ("changed", "original")
    assert ReplayScheduler(root, history, settings()).result == live.result


@pytest.mark.parametrize("transport", ["task", "channel", "latch", "group", "scope"])
def test_pure_helper_wrappers_preserve_the_causal_frontier(transport: str) -> None:
    async def old(ctx: ReplayFlow) -> object:
        await ctx.call(ask("A"))
        return await ctx.call(ask("B"))

    async def changed(ctx: ReplayFlow) -> object:
        channel = ctx.channel()
        latch = ctx.latch()
        group = ctx.group()

        async def helper(child: ReplayFlow) -> object:
            reply = await child.call(ask("A"))
            channel.send(reply)
            latch.set(reply)
            return reply

        if transport == "scope":
            async with group:
                group.start(helper)
        else:
            task = group.start(helper)
            if transport == "task":
                await task
            elif transport == "channel":
                await channel
            elif transport == "latch":
                await latch
            else:
                await group
        return await ctx.call(ask("B"))

    live = ReplayScheduler(old, (), settings())
    history = finish(live, ["a", "b"])
    replayed = ReplayScheduler(changed, history, settings())
    assert replayed.result == live.result
    assert replayed.frontier().unused == ()
    assert replayed.dispatches() == ()


def test_group_is_a_source_in_finishing_order() -> None:
    async def root(ctx: ReplayFlow) -> object:
        group = ctx.group()

        async def a(child: ReplayFlow) -> object:
            return (await child.call(ask("A"))).root

        async def b(child: ReplayFlow) -> object:
            return (await child.call(ask("B"))).root

        group.start(a)
        group.start(b)
        return (await group, await group)

    live = ReplayScheduler(root, (), settings())
    a, b = live.dispatches()
    history = [accepted(b, 1, "b"), accepted(a, 2, "a")]
    for item in history:
        live.accept(item)
    assert live.result == ("b", "a")
    assert ReplayScheduler(root, history, settings()).result == live.result


@pytest.mark.parametrize("source_kind", ["task", "latch", "timer"])
def test_sources_do_not_yield_a_value_twice(source_kind: str) -> None:
    async def root(ctx: ReplayFlow) -> object:
        async def done(_: ReplayFlow) -> object:
            return "done"

        if source_kind == "task":
            source = ctx.group().start(done)
        elif source_kind == "latch":
            source = ctx.latch()
            source.set("done")
        else:
            source = ctx.timer(1)
        await source
        return await source

    if source_kind == "timer":
        run = ReplayScheduler(root, (), settings())
        with pytest.raises(ReplayDeadlockError, match="no producer"):
            run.accept(AcceptedClock(1, 1, settings().initial_allowance))
        run.close()
    else:
        with pytest.raises(ReplayDeadlockError, match="no producer"):
            ReplayScheduler(root, (), settings())


def test_typed_prompt_context_policy_is_excluded_when_scheduler_adds_dependencies() -> None:
    def root(wording: str) -> Root:
        async def flow(ctx: ReplayFlow) -> object:
            await ctx.call(ask("before"))
            context = Context(candidate="a", wording=wording)
            call = ReplayCall(
                SemanticKey("prompt"),
                classify_context(context, contract="plan.v1"),
                "plan.v1",
                ask("prompt", policy=wording.encode()).policy,
                Reply,
            )
            return await ctx.call(call)

        return flow

    live = ReplayScheduler(root("old"), (), settings())
    history = finish(live, ["a", "b"])
    replayed = ReplayScheduler(root("new wording"), history, settings())
    assert replayed.result == live.result
    assert replayed.frontier().drift.count == 1
    assert replayed.dispatches() == ()


@given(
    lengths=st.lists(st.integers(min_value=1, max_value=5), min_size=1, max_size=4),
    choices=st.lists(st.integers(min_value=0, max_value=100), min_size=1, max_size=20),
)
def test_generated_causal_digests_match_an_independent_ordered_observation_model(
    lengths: list[int], choices: list[int]
) -> None:
    received = []

    async def root(ctx: ReplayFlow) -> object:
        await ctx.call(ask("seed"))
        reports = ctx.channel()
        group = ctx.group()

        def worker(branch: int) -> Root:
            async def child(child_ctx: ReplayFlow) -> object:
                for sample in range(lengths[branch]):
                    await child_ctx.call(ask(f"branch.{branch}.{sample}"))
                    reports.send((branch, sample))
                return None

            return child

        for branch in range(len(lengths)):
            group.start(worker(branch))
        for _ in range(sum(lengths)):
            branch, sample = await reports
            received.append((branch, sample))
            await ctx.call(ask(f"planning.{branch}.{sample}"))
        await group.all()
        return await ctx.call(ask("final"))

    live = ReplayScheduler(root, (), settings())
    model = LinearCausality(len(lengths))
    saved = []
    while not live.completed:
        starts = live.dispatches()
        for start in starts:
            model.check(start)
        chosen = starts[choices[len(saved) % len(choices)] % len(starts)]
        assert chosen.key is not None
        observation = accepted(chosen, len(saved) + 1, f"reply-{len(saved)}")
        model.accept(observation)
        saved.append(observation)
        live.accept(observation)
    expected_order = tuple(received)
    received.clear()
    replayed = ReplayScheduler(root, saved, settings())
    assert tuple(received) == expected_order
    assert replayed.result == live.result
    assert not replayed.frontier().unused


def test_drift_notice_names_both_historical_and_current_roles_and_templates() -> None:
    async def old(ctx: ReplayFlow) -> object:
        return await ctx.call(ask("one"))

    async def changed(ctx: ReplayFlow) -> object:
        call = ask("one", policy=b"new")
        call.policy.role = "reviewer"
        call.policy.template = "review.j2"
        return await ctx.call(call)

    live = ReplayScheduler(old, (), settings())
    saved = finish(live, ["a"])
    run = ReplayScheduler(changed, saved, settings())
    assert run.frontier().drift.count == 1
    assert run.frontier().drift.roles == ("planner", "reviewer")
    assert run.frontier().drift.templates == ("plan.j2", "review.j2")
    assert run.result == live.result
